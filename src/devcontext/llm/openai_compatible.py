from devcontext.llm.chat_completions import ChatCompletionsLLMClient


class ReasoningPrefixFilter:
    """Remove only a gateway's leading think block, across arbitrary chunks.

    Once answer text starts, literal tags in code or examples are preserved.
    An unfinished reasoning block is an error, never a complete answer.
    """
    def __init__(self):
        self.buffer = ""
        self.mode = "prefix"

    def feed(self, text):
        if self.mode == "answer":
            return text
        self.buffer += text
        if self.mode == "prefix":
            candidate = self.buffer.lstrip()
            if not candidate or "<think>".startswith(candidate):
                return ""
            if candidate.startswith("<think>"):
                self.buffer = candidate[len("<think>"):]
                self.mode = "thinking"
            else:
                self.mode = "answer"
                result, self.buffer = self.buffer, ""
                return result
        if self.mode == "thinking":
            end = self.buffer.find("</think>")
            if end < 0:
                # Retain only enough to detect a split closing tag.
                self.buffer = self.buffer[-len("</think>"):]
                return ""
            self.mode = "answer"
            result, self.buffer = self.buffer[end + len("</think>"):], ""
            return result
        return ""

    def finish(self):
        if self.mode == "thinking" or (self.mode == "prefix" and self.buffer.strip()):
            raise RuntimeError("OpenAI-compatible returned an incomplete reasoning prefix")
        result, self.buffer = self.buffer, ""
        return result


class OpenAICompatibleLLMClient(ChatCompletionsLLMClient):
    provider = "openai"
    display_name = "OpenAI-compatible"
    config_prefix = "OPENAI"
    output_token_parameter = "max_completion_tokens"
    supported_reasoning_efforts = {"low", "medium", "high", "max"}

    def __init__(self, api_key: str, base_url: str, model: str = "gpt-6.1-sol", **kwargs):
        super().__init__(api_key=api_key, base_url=base_url, model=model, **kwargs)

    def _generate(self, messages):
        if self.json_mode:
            return self._generate_json_stream(messages)
        response = super()._generate(messages)
        prefix = ReasoningPrefixFilter()
        answer = (prefix.feed(response) + prefix.finish()).strip()
        if not answer:
            raise RuntimeError("OpenAI-compatible returned an empty answer after reasoning")
        return answer

    def _generate_json_stream(self, messages):
        # Gateways can return 524 while buffering long non-streaming blueprints.
        # Keep one HTTP/LLM call, accumulate SSE internally, and expose only the
        # complete JSON to the existing strict Planner parser and call recorder.
        import time
        from devcontext.deadline import bounded_timeout
        from devcontext.llm.streaming import curl_stream, parse_sse, StreamFailure
        original_timeout = self.timeout_seconds
        self.timeout_seconds = bounded_timeout(original_timeout)
        source = curl_stream(self, messages)
        prefix = ReasoningPrefixFilter()
        parts = []
        self.last_partial_response = ""
        started = time.perf_counter()
        try:
            for event in parse_sse(source):
                if event.type == "content":
                    parts.append(prefix.feed(event.text))
                elif event.type == "usage":
                    self.last_usage.update(event.usage or {})
                elif event.type == "finish":
                    self.last_finish_reason = event.finish_reason
                    if event.finish_reason != "stop":
                        raise StreamFailure(f"JSON generation did not complete normally: {event.finish_reason}")
                    parts.append(prefix.finish())
            if self.last_finish_reason != "stop":
                raise StreamFailure("JSON stream missing normal finish")
            answer = "".join(parts).strip()
            if not answer:
                raise StreamFailure("JSON stream returned an empty answer")
            return answer
        finally:
            self.last_partial_response = "".join(parts)
            source.close()
            self.timeout_seconds = original_timeout
            self.last_latency_ms = (time.perf_counter() - started) * 1000
