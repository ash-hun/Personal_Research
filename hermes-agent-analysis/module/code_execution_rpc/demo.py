"""
demo.py — Run with: python3 demo.py   (no external deps, stdlib only)

Demonstrates Hermes's "code execution via RPC" mirror:
  1. Register 3 fake tools (fetch, parse, summarize) on a ToolGateway.
  2. The "agent" submits ONE Python script that chains them via RPC stubs,
     with plain Python transforming data between calls.
  3. execute_code() runs the script; we print the RPC calls it made and the
     final result, then contrast with the multi-turn alternative.
"""

from code_execution_rpc import ToolGateway, execute_code


# ---------------------------------------------------------------------------
# 1) Three fake tools. Each handler takes a JSON-able args dict, returns a
#    JSON-able result — exactly the ToolHandler contract the gateway expects.
# ---------------------------------------------------------------------------

_FAKE_WEB = {
    "https://news.example/ai": "Nous Research ships Hermes 4. Agents now write "
    "code that calls tools over RPC. This collapses long tool pipelines into a "
    "single zero-context-cost turn. Researchers love it.",
    "https://news.example/ml": "A new sandbox runtime exposes tools as callable "
    "stubs. Scripts loop, filter, and branch before any result re-enters the "
    "model context. Token usage drops sharply.",
}


def tool_fetch(args):
    """fetch(url) -> {"url", "html"}: pretend HTTP GET."""
    url = args["url"]
    return {"url": url, "html": _FAKE_WEB.get(url, "")}


def tool_parse(args):
    """parse(html) -> {"sentences": [...]}: naive sentence splitter."""
    html = args["html"]
    sentences = [s.strip() for s in html.split(".") if s.strip()]
    return {"sentences": sentences}


def tool_summarize(args):
    """summarize(sentences, max_words) -> {"summary"}: first-N-words extract."""
    sentences = args["sentences"]
    max_words = args.get("max_words", 20)
    words = " ".join(sentences).split()
    return {"summary": " ".join(words[:max_words]) + ("..." if len(words) > max_words else "")}


def build_gateway() -> ToolGateway:
    gw = ToolGateway()
    gw.register("fetch", tool_fetch, signature="url: str",
                doc="Fetch a URL. Returns {url, html}.")
    gw.register("parse", tool_parse, signature="html: str",
                doc="Split html into sentences. Returns {sentences}.")
    gw.register("summarize", tool_summarize, signature="sentences: list, max_words: int = 20",
                doc="Summarize sentences. Returns {summary}.")
    return gw


# ---------------------------------------------------------------------------
# 2) The agent's single-turn script. It chains fetch -> parse -> summarize
#    across TWO urls, with Python (a loop + dict building) doing the glue.
#    Only the final print() re-enters the model context.
# ---------------------------------------------------------------------------

AGENT_SCRIPT = '''
# Agent-authored script: one turn, many tool calls, zero extra context cost.
urls = ["https://news.example/ai", "https://news.example/ml"]

summaries = {}
for url in urls:
    page = tools.fetch(url=url)                       # RPC call 1, 3
    parsed = tools.parse(html=page["html"])           # RPC call 2, 4
    result = tools.summarize(                          # RPC call 5, 6
        sentences=parsed["sentences"], max_words=12
    )
    summaries[url] = result["summary"]

# Plain-Python post-processing before anything hits the model context:
report = {
    "pages_processed": len(summaries),
    "summaries": summaries,
}
print(json.dumps(report, indent=2, ensure_ascii=False))
'''


def main() -> int:
    gateway = build_gateway()

    print("=" * 70)
    print("TOOL DISCOVERY (what the agent sees before writing the script)")
    print("=" * 70)
    for spec in gateway.describe_tools():
        print(f"  tools.{spec['signature']:<46} - {spec['doc']}")

    print("\n" + "=" * 70)
    print("RUNNING execute_code() — one agent turn, one script")
    print("=" * 70)
    result = execute_code(AGENT_SCRIPT, gateway)

    print("\n--- RPC calls the script made (round-tripped through the gateway) ---")
    for i, entry in enumerate(result.call_log, 1):
        print(f"  {i:>2}. tools.{entry.tool}({entry.args_preview})")

    print("\n--- Script result (the ONLY thing that re-enters model context) ---")
    print(result.output)

    print("--- execute_code envelope ---")
    print(f"  status           : {result.status}")
    print(f"  tool_calls_made  : {result.tool_calls_made}")
    print(f"  duration_seconds : {result.duration_seconds}")

    print("\n" + "=" * 70)
    print("CONTRAST: same work WITHOUT code execution")
    print("=" * 70)
    n = result.tool_calls_made
    print(f"  With execute_code : 1 agent turn  (1 script, {n} RPC calls inline)")
    print(f"  Without it        : {n} agent turns (each tool call = a separate")
    print(f"                      LLM round-trip; every intermediate html/sentence")
    print(f"                      list lands in the context window).")
    print(f"  Context saved     : {n - 1} extra turns + all intermediate payloads.")

    # Exit non-zero if the pipeline didn't behave, so CI/`python3 demo.py` is a check.
    ok = (
        result.status == "success"
        and result.tool_calls_made == 6
        and "pages_processed" in result.output
    )
    print("\nDEMO", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
