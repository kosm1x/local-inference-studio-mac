# models/

Model payloads (`*.gguf` and anything else under here except this file and
`manifest.json`) are gitignored and never committed.

`manifest.json` lists the files `scripts/serve.sh` may serve.

## Adding an entry

1. Download the GGUF by hand into `models/<dir>/<file>.gguf` (the scripts never
   download anything).
2. Record its size and digest from the file on disk:

   ```
   stat -f %z models/<dir>/<file>.gguf
   shasum -a 256 models/<dir>/<file>.gguf
   ```

   Compare the digest with the publisher's (for Hugging Face, the LFS sha256 the file
   page lists). If they differ, delete the file.
3. Append one object to `files`:

   ```json
   {
     "id": "<short-name>",
     "path": "models/<dir>/<file>.gguf",
     "repo": "<hf-user>/<hf-repo>",
     "revision": "<commit the file was fetched from>",
     "file": "<file>.gguf",
     "size": 12345678901,
     "sha256": "<64 lowercase hex>"
   }
   ```

   - `id`: letters, digits, `.`, `_`, `-`; unique in the list.
   - `path`: repo-relative, no `..`, ends in `.gguf`.
   - `size`: integer bytes. `sha256`: 64 lowercase hex characters.
   - `default` (optional, as in the Windows reference) is informational; the model
     actually served is the one `config/lis.conf` names.
   - Any other field name refuses the start (a typo is never silently ignored).
4. Optional per-model settings (see below).
5. Set `MODEL_ID=<short-name>` in `config/lis.conf`. A model that needs an explicit
   template file gets `chat_template` in its entry (below), so switching `MODEL_ID`
   switches the template too.
6. `scripts/serve.sh --dry-run` prints the command line; the first real start hashes the
   file (once per boot) and refuses on a size or sha256 mismatch.

## Per-model settings (optional fields)

```json
{
  "id": "qwen3.6-moe",
  "...": "...",
  "kv_type": "f16",
  "sampling": {"temp": 0.7, "top_p": 0.8, "top_k": 20},
  "reasoning": "auto",
  "chat_template": "config/chat-templates/<name>.jinja",
  "server_args": ["--swa-full"]
}
```

- `kv_type`: KV cache type for this model, overriding `KV_TYPE` in `config/lis.conf`.
  One of `f32 f16 bf16 f8_e4m3 q8_0 q4_0 q4_1 iq4_nl q5_0 q5_1` (the values llama-server
  1.0.0 lists for both `-ctk` and `-ctv`; `f8_e4m3` is new in 1.0.0 and a model run
  with it is UNVERIFIED here). `f16` is the unquantized default; it is still passed
  explicitly as `-ctk f16 -ctv f16`.
- `reasoning`: the `--reasoning` value for this model, one of the strings `off`, `on`,
  `auto`. Precedence: `scripts/serve.sh --reasoning ...` when given, else this field,
  else `off`. A given flag always wins, `--reasoning off` included.
- `chat_template`: a repo-relative path to a `.jinja` file under `config/`, passed as
  `--chat-template-file`. When present it is used instead of `CHAT_TEMPLATE` in
  `config/lis.conf`; when absent `CHAT_TEMPLATE` applies (empty = the template embedded
  in the GGUF). It passes the same checks as `CHAT_TEMPLATE`: only `A-Z a-z 0-9 . _ / -`,
  no leading `/`, no `..`, `.` or empty component, not a symlink, physically inside
  `config/` (no symlinked directory leading out), and the file must exist. A set
  `CHAT_TEMPLATE` is still checked when this field replaces it.
- `sampling`: an object with any of `temp` (number >= 0), `top_p` (number from 0 to 1)
  and `top_k` (integer >= 0). Each key given replaces that default (0.6, 0.95, 20); a
  key left out keeps its default. Numbers only: strings, booleans, `NaN` and
  `Infinity` refuse the start, and so does any other key.
- `server_args`: a list of non-empty strings without control characters, appended to
  the command line after the fixed flags and before any `scripts/serve.sh -- ...`
  arguments, one element per argument (put a flag and its value in separate elements;
  an element holding a space refuses). They pass the same allow-list as the `--`
  arguments, below.

A malformed entry (wrong type, value out of range, unknown field) refuses the start,
`--dry-run` included, and the message names the entry and the field. The model file must sit inside `models/` physically: a symlinked
`.gguf`, or a symlinked `models/` or subdirectory that leads outside it, refuses the
start.

## Extra llama-server flags (allow-list)

`server_args` and `scripts/serve.sh -- ...` may only hold these flags of the installed
build (llama-server 1.0.0, build 12359, commit b1b770d90, rechecked against its
`--help` on 2026-11-14; `ALLOWED_FLAGS` in `scripts/serve.sh`), each
with a value of the kind shown. Anything else refuses the start and the message names
the flag.

| Value | Flags |
| --- | --- |
| whole number 0-9999999 | `-t` `--threads`, `-tb` `--threads-batch`, `-b` `--batch-size`, `-ub` `--ubatch-size`, `--cache-reuse`, `-ctxcp` `--ctx-checkpoints` `--swa-checkpoints`, `-cms` `--checkpoint-min-step`, `-cram` `--cache-ram`, `-ncmoe` `--n-cpu-moe`, `-fitc` `--fit-ctx`, `--threads-http` |
| whole number, `auto` or `all` | `-ngl` `--gpu-layers` `--n-gpu-layers` |
| `on` or `off` (required) | `-fit` `--fit` |
| whole numbers joined by commas | `-fitt` `--fit-target` |
| `auto` `none` `mmap` `mlock` `mmap+mlock` `dio` | `-lm` `--load-mode` |
| 0-100 | `--poll` |
| 0-3 | `-lv` `--verbosity` `--log-verbosity` |
| none | `--swa-full`, `-cmoe` `--cpu-moe`, `--warmup` `--no-warmup`, `-kvu` `--kv-unified` `-no-kvu` `--no-kv-unified`, `-kvo` `--kv-offload` `-nkvo` `--no-kv-offload`, `--repack` `-nr` `--no-repack`, `--op-offload` `--no-op-offload`, `--cache-idle-slots` `--no-cache-idle-slots`, `--log-timestamps` `--no-log-timestamps`, `--log-prefix` `--no-log-prefix` |

- Values never start with `-` (so `-1` = "unlimited" is not available) and are decimal.
- `--flag=value` refuses: this build rejects that form.
- `--long_flag` is accepted as `--long-flag`, the way the binary reads it, and is passed
  on in the hyphen spelling.
- Not available on purpose: anything that loads weights or other files (`-m`, `-md`,
  `--lora`, `--mmproj`, `--control-vector`, `--grammar-file`, ...), network, TLS and
  CORS flags, flags that write a path or run a program, `-v`/`--verbose` and
  verbosity 4-5, and the flags `serve.sh` already sets (context size, slots, alias,
  sampling, chat template, `--offline`). Sampling and KV type come from `sampling` and
  `kv_type`.
- `serve.sh` always passes `--offline` (this build's `--help`: "prevents network
  access"); the model is a local file from this manifest, so nothing is fetched. A full
  model load with `--offline` is UNVERIFIED until a model exists.
