# Provider compatibility (M2)

Historical raw reports and logs remain local archives. See the [public validation summary](validation/README.md) for their scope and reproducible entry points.

The default provider is the offline, scriptable `FakeModel`. No API key is needed for
development or the repair demonstration. `doctor --offline` only inspects local
configuration and executables; it never tests model connectivity.

## DeepSeek Flash setup

Copy `.env.example` to `.env` in the project root and fill in:

```dotenv
STELLARWEAVER_API_KEY=your_key_here
```

The key stays in the launcher directory. Target repositories do not need a copy.
The process environment takes precedence; otherwise the launcher directory's `.env`
is used, then the workspace `.env` if no launcher file exists. `--env-file PATH`
selects a different credential file. Dotenv values are read without exporting them
to child processes. `.env` is ignored by Git and protected from built-in file tools.

```powershell
uv run stellarweaver doctor --offline
uv run stellarweaver doctor --check-model --provider deepseek
uv run stellarweaver chat --provider deepseek --stream --workspace D:\path\to\your\repo
```

For persistent settings, copy `stellarweaver.example.toml` to `stellarweaver.toml`
in the target workspace, or pass `--config PATH`. The example chooses
`deepseek-flash` at `https://api.deepseek.com`. The configured context window now defaults to 100,000, with a per-task cumulative
input budget of 1,000,000. The 4,096 output reserve and 2,048 safety margin leave
93,856 for estimated input. Counts use conservative UTF-8 JSON bytes, not a matching
model tokenizer; this is an application setting, not a claim about the model's
maximum capacity. Explicit workspace/--config values take precedence. Historical
M2/M5 reports retain their original 32,768 configuration and are not rerun by this change.

## Tested protocol boundary

The adapter uses the official OpenAI Python SDK's Chat Completions API with
non-streaming responses and streamed SSE responses. DeepSeek requests explicitly disable thinking:
`{"thinking":{"type":"disabled"}}`. It supports text, multiple function calls,
unaltered JSON argument strings, corresponding tool-result IDs, and public token
usage. Hidden reasoning fields are neither read into domain types nor persisted
or sent back. Vendor-specific options stay inside the adapter. No
`parallel_tool_calls` option is sent, and the runtime executes tools serially.

Tests use the real installed SDK with `httpx.MockTransport`. They verify outbound
request JSON, multiple tool calls, tool-result ID pairing, absent usage,
`finish_reason=length`, capability rejection, and sanitized HTTP failures. SSE
fixtures split UTF-8 and delimiters at byte boundaries and interleave tool indexes,
function-name fragments, argument fragments and visible text. They also cover
usage-only chunks, ID changes and duplicates, truncated/invalid streams, transport
disconnects, bounded aggregation, and content after the finish marker. These are
**transport-fixture tests, not real DeepSeek connectivity tests**. Separate live
results are recorded below.

Only visible text deltas reach the UI before completion. Function arguments stay
unaltered and are not parsed, authorized or executed until the whole response has
finished. Interrupted streams do not commit an assistant/tool round. Length-truncated
responses remain incomplete evidence and cannot execute tools. A provider configured
without streaming support falls back to one non-streaming request. `--stream` and
`--no-stream` provide explicit CLI selection.

SDK retries are disabled (`max_retries=0`), including for injected test clients.
The runtime owns retry accounting: every HTTP attempt counts as a model request;
eligible network failures, 408/409/429 and 5xx responses allow at most two additional
attempts by default. Retries obey exponential backoff, jitter, Retry-After seconds or
HTTP-date, request count, token limits and the remaining wall budget. Authentication,
invalid-request and unknown-model HTTP errors are not retried. Retrying a model
request does not rerun previous tools, and tools have no automatic retry. HTTP errors
record a sanitized status without the vendor body, key or SDK exception string.

Public usage is accumulated when provided. Missing usage remains unknown as a billing
fact; conservative UTF-8-based estimates still constrain application budgets and set
`usage_estimated=true`. Interrupted requests without complete usage reserve the input
estimate and output allowance, additionally setting `usage_incomplete=true`. These
estimates must not be reported as provider-confirmed token charges.

Official references checked on 2026-10-25:

- [DeepSeek models and pricing](https://api-docs.deepseek.com/quick_start/pricing/)
- [DeepSeek tool calls](https://api-docs.deepseek.com/guides/tool_calls/)
- [OpenAI Python SDK](https://github.com/openai/openai-python)

`openai-compatible-chat` can explicitly select another configured endpoint, but
no other service is claimed compatible or verified. Audio, images, native Responses,
and hidden reasoning are outside the tested M2 interface.

## Offline scripts

`--provider fake --script replies.json` reads a strict JSON array of replies:

```json
[
  {
    "text": "Read the source",
    "tool_calls": [
      {"call_id": "read-1", "name": "read_file", "arguments_json": "{\"path\":\"parser.py\"}"}
    ],
    "stop_reason": "tool_calls"
  },
  {"text": "Offline script complete; no test was run.", "stop_reason": "stop"}
]
```

Python callers can supply a list of `AssistantReply` objects or a synchronous or
asynchronous `callback(ModelRequest) -> AssistantReply`. `FakeModel.requests`
records every full request, making actual tool evidence available to the next
scripted decision. An exhausted script is an explicit provider failure. Its stream
interface records the request once, emits visible text when present and produces one
completed reply; it does not simulate paid-model connectivity or coding ability.

## Live connectivity check: 2026-10-25

After the user configured the key and explicitly requested a connectivity check,
one non-streaming request through the existing adapter succeeded against
api.deepseek.com using deepseek-flash. The reply exactly matched
STELLARWEAVER_DEEPSEEK_OK with finish_reason=stop. Elapsed time: 2.063 seconds;
reported usage: 32 prompt + 11 completion = 43 total tokens.

No tools were exposed or executed and no repository content was sent. This only
verifies this authenticated text request, not a real tool loop, coding task or
streaming compatibility. See the recorded response（本地归档 `docs/validation/deepseek_connectivity.json`）.

## M2 live streaming task: 2026-10-25

One real `deepseek-flash` streaming run completed a small pagination repair in a
temporary Git repository. The Agent read the actual files, observed a failing test
command (exit 1), edited `pagination.py` with its previously read SHA-256, and reran
the command successfully (exit 0). A separate oracle then passed 14 assertions.
The visible test file and an unrelated fixture retained their original hashes.
All 10 assistant tool calls had corresponding results.

The run used 7 model requests and 10 tool calls, with 31,568 input tokens and 1,251
output tokens reported by the provider. Its recorded duration was 13.938 seconds;
`usage_estimated=false` and `usage_incomplete=false`. The runtime recorded
`run_status=completed` and `verification_status=passed`, while leaving
`task_assessment=unknown`; the external oracle, rather than the model's final
statement, supplies the independent success evidence. See the
live report（本地归档 `docs/validation/m2_live_report.json`） and
recorded events（本地归档 `docs/validation/m2_live_report_events.json`）.

`doctor --check-model --provider deepseek` also succeeded using an explicit
instruction to reply with a fixed marker. It is a credential/text-response check
with no exposed tools. An earlier ambiguous marker-only probe did not satisfy its
response check; the prompt was made explicit before the successful retest.
`doctor --offline` continues to make no model requests.

This establishes one authenticated streaming tool loop and one small task result
for the configured endpoint/model on this date. It does not establish broad coding
success rates, general compatibility with other services, production reliability,
or an independent evaluation across many tasks. Live rate-limit retries, actual
network-interruption recovery and other operating systems were not exercised by
this successful run; their relevant protocol branches have offline tests.

## M5 真实任务补充（2026-10-25）

DeepSeek deepseek-flash已显式完成固定20任务一次，SSE 93次请求，工具117次，输入319974/输出16757 token均由服务端usage报告。20次Agent运行completed；初评19/20、修正Q01无关读取要求后零模型重评分20/20。完整范围/分母/单次限制及原始报告见[评测结果](../evals/RESULTS.md)。未测试其他模型、原生Responses或HTTP/OAuth MCP。
