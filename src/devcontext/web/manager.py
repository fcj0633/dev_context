import asyncio
from dataclasses import replace
from devcontext.delivery import DeliveryError
from devcontext.delivery.buffer import BufferedDelivery
from concurrent.futures import ThreadPoolExecutor, wait
from threading import RLock

from devcontext.application import AskRequest, AskService
from devcontext.config import Settings
from devcontext.execution import CancellationToken, RequestCancelled
from .protocol import PublicData, RunError, TERMINAL, evidence_items, execution_summary


class RunManager:
    def __init__(self, store, settings=None, service=None, max_workers=2):
        self.store = store
        self.settings = settings or Settings()
        self.service = service or AskService(self.settings)
        self.public = PublicData(self.settings)
        self.preview = self.settings.web_stream_preview_enabled
        self.max_workers = max_workers
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="devcontext-ask")
        self.lock = RLock()
        self.active = {}
        self.futures = {}
        self.subscribers = {}
        self.closing = False
        self.storage_failures = set()

    def notify(self, run_id):
        with self.lock:
            targets = tuple(self.subscribers.get(run_id, ()))
        for loop, event in targets:
            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(event.set)
                except RuntimeError:
                    pass

    def subscribe(self, run_id):
        pair = (asyncio.get_running_loop(), asyncio.Event())
        with self.lock:
            self.subscribers.setdefault(run_id, set()).add(pair)
        return pair

    def unsubscribe(self, run_id, pair):
        with self.lock:
            targets = self.subscribers.get(run_id)
            if targets is not None:
                targets.discard(pair)
                if not targets:
                    self.subscribers.pop(run_id, None)

    def submit(self, session_id, query, profile, client_request_id):
        query = self.public.clean(query)
        with self.lock:
            existing = self.store.existing(session_id, client_request_id)
            if existing:
                if (existing["query"], existing["profile"]) != (query, profile):
                    raise RunError("idempotency_conflict")
                return existing
            if self.closing:
                raise RunError("service_stopping", 503)
            self.store.session(session_id)
            if any(session_id == sid for sid, _ in self.active.values()):
                raise RunError("session_busy")
            if len(self.active) >= self.max_workers:
                raise RunError("capacity_exceeded", 429)
            run = self.store.create_run(session_id, query, profile, client_request_id)
            run_id = run["id"]
            token = CancellationToken()
            self.active[run_id] = (session_id, token)
            try:
                self.futures[run_id] = self.pool.submit(self._execute, run, token)
            except Exception:
                self.active.pop(run_id, None)
                self.store.finish(run_id, "failed", "execution_failed", self._error("execution_failed"))
                self.store.release_worker(run_id)
                raise RunError("execution_failed", 503)
            return run

    def get(self, run_id):
        with self.lock:
            if run_id in self.storage_failures:
                raise RunError("storage_unavailable", 503)
        return self.store.run(run_id)

    def delete_session(self, session_id):
        with self.lock:
            if any(session_id == sid for sid, _ in self.active.values()):
                raise RunError("session_busy")
            self.store.delete(session_id)

    def cancel(self, run_id):
        with self.lock:
            run = self.get(run_id)
            if run["status"] not in TERMINAL:
                self.store.finish(run_id, "cancelled", "user_cancelled")
                if run_id in self.active:
                    self.active[run_id][1].cancel()
                self.notify(run_id)
            return self.get(run_id)

    def _emit(self, run_id, kind, data):
        event = self.store.emit(run_id, kind, self.public.clean(data))
        if event:
            self.notify(run_id)

    @staticmethod
    def _error(code):
        messages = {"execution_failed": "执行失败，请检查模型和检索服务配置后重试。",
                    "generation_interrupted": "回答生成中断，已保留完成的章节和未完成内容。",
                    "generation_failed": "尚未完成回答，请结合已保留内容核实或重试。",
                    "retrieval_failed": "检索未能完成，请检查检索服务。已有章节仅供参考。",
                    "llm_failed": "模型调用失败，请检查模型服务后重试。",
                    "request_timeout": "请求达到时间限制，请重试。",
                    "answer_mismatch": "章节与最终回答不一致，已保留完成的章节。"}
        return {"code": code, "message": messages.get(code, "服务暂时无法完成请求。")}

    def _execute(self, run, token):
        run_id = run["id"]
        manager = self

        class Observer:
            def on_action_completed(self, action, execution):
                token.check()
                manager._emit(run_id, "retrieval.action_completed", {"strategy": execution.strategy,
                    "source_type": action.source_scope, "result_count": len(execution.results),
                    "latency_ms": execution.timings.total_ms, "round_index": action.round_index})

            def on_context_built(self, round_index, context):
                token.check()
                manager._emit(run_id, "retrieval.context_built", {"round_index": round_index,
                    "item_count": len(context.items), "truncated": context.truncated})

        delivery = None

        def section(value):
            token.check()
            if delivery is not None and delivery.started:
                return
            event = self.store.section(run_id, self.public.clean({"section_id": value.section_id,
                                       "markdown": value.markdown, "citations": list(value.citations)}))
            if event:
                self.notify(run_id)

        def evidence(bundle):
            token.check()
            self.store.evidence(run_id, self.public.clean(evidence_items(bundle)), replace=True)
            self.notify(run_id)

        published_body = False
        preview_text = {}

        def persist(event):
            nonlocal published_body
            cleaned = replace(event, data=self.public.clean(event.data))
            if self.store.answer_event(run_id, cleaned):
                if event.type == "answer.generation_started":
                    preview_text.clear()
                if event.type == "answer.text_delta" and not published_body:
                    import re
                    key = (event.generation_id, event.data["section_id"])
                    preview_text[key] = preview_text.get(key, "") + cleaned.data["delta"]
                    body = re.sub(r"(?m)^\s*#{1,6}[^\n]*(?:\n|$)", "", preview_text[key])
                    if body.strip():
                        from devcontext.observability.diagnostics import mark
                        mark("first_body_published")
                        published_body = True
                        preview_text.clear()
                self.notify(run_id)

        try:
            token.check()
            if self.preview:
                delivery = BufferedDelivery(persist, secrets=self.public.secrets, check=token.check)
            self._emit(run_id, "run.started", {"status": "running"})
            result = self.service.ask(AskRequest(run["query"], profile=run["profile"]), observer=Observer(),
                                      on_section=section, on_evidence=evidence, cancellation=token,
                                      **({"on_event": delivery.submit} if delivery else {}))
            if delivery is not None:
                delivery.drain()
            token.check()
            snapshot = self.store.run(run_id)
            summary = self.public.clean(execution_summary(result.trace))
            generation = (result.trace.teaching or {}).get("completion_status")
            final_answer = self.public.clean(result.answer_result.answer)
            joined = "\n\n".join(s["markdown"] for s in snapshot["sections"])
            reason = error = None
            if not snapshot["sections"]:
                status = "failed"
                reason = "retrieval_failed" if result.trace.evidence_package_state == "RETRIEVAL_FAILED" else "generation_failed"
                error = self._error(reason)
                final_answer = ""
            elif joined != final_answer:
                status, reason = "partial", "answer_mismatch"
                error, final_answer = self._error(reason), joined
            elif generation != "complete":
                status, reason = "partial", "generation_interrupted"
                error = self._error(reason)
            elif result.trace.evidence_package_state != "READY":
                status = "partial"
                reason = "retrieval_failed" if result.trace.evidence_package_state == "RETRIEVAL_FAILED" else "evidence_insufficient"
                if reason == "retrieval_failed":
                    error = self._error(reason)
            else:
                status = "completed"
            self.store.evidence(run_id, self.public.clean(evidence_items(result.context_bundle, result.evidence_catalog)), replace=True)
            self.store.finish(run_id, status, reason, error, summary, final_answer)
            self.notify(run_id)
        except RequestCancelled:
            try:
                self.store.finish(run_id, "cancelled", "user_cancelled")
            except Exception:
                with self.lock:
                    self.storage_failures.add(run_id)
            self.notify(run_id)
        except (Exception, DeliveryError) as exc:
            # Never serialize provider exceptions or raw model output.
            try:
                from devcontext.deadline import RequestDeadlineExceeded
                from devcontext.llm.errors import LLMRequestError
                from devcontext.embedding.client import EmbeddingQueryError
                import psycopg
                current = self.store.run(run_id)
                has_answer = bool(current["sections"])
                code = ("request_timeout" if isinstance(exc, RequestDeadlineExceeded) else
                        "llm_failed" if isinstance(exc, LLMRequestError) else
                        "retrieval_failed" if isinstance(exc, (EmbeddingQueryError, psycopg.Error)) else
                        "generation_interrupted" if has_answer else "execution_failed")
                self.store.finish(run_id, "partial" if has_answer else "failed", code, self._error(code))
                self.notify(run_id)
            except Exception:
                with self.lock:
                    self.storage_failures.add(run_id)
                self.notify(run_id)
        finally:
            if delivery is not None:
                try:
                    delivery.close()
                except DeliveryError:
                    with self.lock:
                        self.storage_failures.add(run_id)
                    self.notify(run_id)
            try:
                self.store.release_worker(run_id)
            except Exception:
                with self.lock:
                    self.storage_failures.add(run_id)
            with self.lock:
                self.active.pop(run_id, None)
                self.futures.pop(run_id, None)
            self.notify(run_id)

    def close(self, timeout=30):
        with self.lock:
            self.closing = True
            runs = list(self.active)
            futures = list(self.futures.values())
        for run_id in runs:
            try:
                self.cancel(run_id)
            except Exception:
                with self.lock:
                    if run_id in self.active:
                        self.active[run_id][1].cancel()
        wait(futures, timeout=timeout)
        self.pool.shutdown(wait=False, cancel_futures=True)
