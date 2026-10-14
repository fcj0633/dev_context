"""Archive fixed reference files in ignored artifacts, record source SHA-256."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
SOURCES={
    ("onyx-dot-app/onyx","103300798fa31186794ff1733bc07dc99341f2b7"): [
        "backend/onyx/chat/answer.py","backend/onyx/chat/prompt_builder/answer_prompt_builder.py",
        "backend/onyx/chat/prompt_builder/citations_prompt.py","backend/onyx/agents/agent_search/basic/graph_builder.py",
        "backend/onyx/agents/agent_search/orchestration/nodes/use_tool_response.py","backend/tests/unit/onyx/chat/test_answer.py"],
    ("run-llama/llama_index","5b7b6e323e1da3edd787a2104a89257c14f026d0"): [
        "llama-index-core/llama_index/core/response_synthesizers/simple_summarize.py",
        "llama-index-core/llama_index/core/response_synthesizers/compact_and_refine.py",
        "llama-index-core/tests/response_synthesizers/test_simple_summarize.py"],
    ("openai/openai-agents-python","a30c32e722a4cdb761fe8b250606757ec8581296"): [
        "src/agents/run.py","src/agents/_run_impl.py","tests/test_agent_runner.py","tests/test_agent_runner_streamed.py"],
    ("continuedev/continue","5522c6f44ca0ac3528b37244818fbfa39b5af470"): [
        "core/context/retrieval/retrieval.ts","core/context/retrieval/pipelines/BaseRetrievalPipeline.ts",
        "core/context/retrieval/pipelines/RerankerRetrievalPipeline.ts",
        "core/context/retrieval/pipelines/NoRerankerRetrievalPipeline.ts"],
}


def download(item):
    repo,commit,path=item
    url=f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"
    data=urlopen(Request(url,headers={"User-Agent":"DevContext-fixed-reference"}),timeout=30).read()
    output=ROOT/"artifacts/refactor-stage4/references"/repo.split("/")[-1]/path
    output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(data)
    return {"repository":repo,"commit":commit,"path":path,"sha256":sha256(data).hexdigest(),
        "url":f"https://github.com/{repo}/blob/{commit}/{path}","local_copy":str(output.relative_to(ROOT)).replace("\\","/")}


if __name__=="__main__":
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows=list(executor.map(download,[(repo,commit,path) for (repo,commit),paths in SOURCES.items() for path in paths]))
    output=ROOT/"artifacts/refactor-stage4/references/manifest.json"
    output.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Archived {len(rows)} fixed source files; all SHA-256 recorded")
