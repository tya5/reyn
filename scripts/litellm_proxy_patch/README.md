# litellm proxy patch (#5620)

Works around a real litellm PROXY-layer defect (D): a client requesting
`stream: false` on a `/v1/responses` call routed to the `chatgpt`
provider still gets raw SSE pass-through instead of a plain JSON
response — see `litellm_proxy_patch.py`'s own module docstring for the
full traced chain.

This is separate from reyn's own in-process litellm patches (retired,
#5620 — see `docs/reference/runtime/litellm-compat-patches.md`). This
directory targets a DIFFERENT litellm install: the owner's own
`junk/litellm` proxy (its own venv, its own pinned litellm version),
not reyn's. **Nothing here imports `reyn`.**

## Install

Run with the TARGET environment's own interpreter — the proxy venv's
`python`, not reyn's:

```sh
/path/to/proxy/venv/bin/python install.py
```

This copies `litellm_proxy_patch.py` into that environment's own
`site-packages/` and drops a single-line `.pth` file
(`zz_reyn_litellm_proxy_patch.pth`, containing exactly
`import litellm_proxy_patch`) next to it. Python's own `site` module
imports that module at every future interpreter startup, before any
proxy code runs — no code change to the proxy's own launch command is
needed.

## Verify

After the proxy has started at least once with the patch installed:

```sh
cat ~/.reyn/litellm-proxy-patch-status.json
```

```json
{
  "pid": 12345,
  "litellm_version": "1.95.0",
  "patched": {"D": true},
  "reached": {"D": 0}
}
```

`patched.D` is measured (a real class-attribute flag read off the
actual patched class), never a restated declaration that the file is
present. `reached.D` counts how many real requests the patch has
actually intercepted since the process started — `0` is expected and
correct until a `stream:false` + `chatgpt`-provider request that would
have hit the defect actually occurs; it is not itself evidence the
patch failed to install.

`reyn doctor` (reyn's own side) reads the same file and reports the
same measured state — see `docs/reference/runtime/litellm-compat-
patches.md`'s own proxy section for what it prints.

## Uninstall

```sh
/path/to/proxy/venv/bin/python install.py --uninstall
```

Removes the patch file and its `.pth` line — including a pre-#5620
hand-placed install (`litellm_patch.py` / `zz_litellm_patch.pth`), if
present, so this genuinely leaves nothing behind. No litellm file is
ever edited on disk by either direction.

## When to remove this entirely

If litellm fixes the upstream routing defect D works around, the
scaffold test (`tests/scaffold/test_5620_litellm_proxy_defects.py`,
pinned to the litellm version this was last verified against) goes RED
— that is GOOD NEWS, not a regression (see that test file's own module
docstring). Remove this whole directory, its own CI leg, and both
`tests/scaffold/test_5620_*.py` / `tests/llm/test_5620_litellm_proxy_
patch_d.py` in the same PR.
