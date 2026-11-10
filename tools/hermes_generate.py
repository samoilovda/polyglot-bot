"""Forced SSH command. Run with the Python runtime of the Hermes installation."""

import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

MAX_PROMPT_BYTES = 32 * 1024


def main(agent_path, provider, model):
    if os.environ.get("SSH_ORIGINAL_COMMAND"):
        raise SystemExit("This key accepts a prompt on stdin only")
    data = sys.stdin.buffer.read(MAX_PROMPT_BYTES + 1)
    if not data.strip() or len(data) > MAX_PROMPT_BYTES:
        raise SystemExit("Expected a non-empty UTF-8 prompt of at most 32 KiB")
    prompt = data.decode("utf-8")

    os.chdir("/")
    sys.path.insert(0, agent_path)
    os.environ.update(HERMES_SAFE_MODE="1", HERMES_IGNORE_USER_CONFIG="1", HERMES_IGNORE_RULES="1")
    with redirect_stdout(sys.stderr), TemporaryDirectory(prefix="polyglot-hermes-") as scratch:
        import hermes_bootstrap  # noqa: F401
        from hermes_cli.config import load_env
        from hermes_cli.runtime_provider import resolve_runtime_with_fallback
        from run_agent import AIAgent

        load_env()
        os.environ["HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS"] = "45"
        runtime, _ = resolve_runtime_with_fallback({}, requested=provider, target_model=model)
        agent = AIAgent(
            model=model,
            provider=runtime.get("provider"),
            requested_provider=runtime.get("requested_provider"),
            api_key=runtime.get("api_key"),
            api_mode=runtime.get("api_mode"),
            base_url=runtime.get("base_url"),
            credential_pool=runtime.get("credential_pool"),
            enabled_toolsets=[],
            skip_context_files=True,
            skip_memory=True,
            skip_background_review=True,
            reasoning_config={"enabled": True, "effort": "low"},
            quiet_mode=True,
        )
        try:
            if agent.valid_tool_names:
                raise SystemExit("Hermes started with tools enabled")
            agent.logs_dir = Path(scratch)
            result = agent.run_conversation(prompt)
        finally:
            agent.close()

    if result.get("failed") or not result.get("final_response"):
        raise SystemExit("Hermes generation failed")
    print(result["final_response"])


if __name__ == "__main__":
    main(*sys.argv[1:])
