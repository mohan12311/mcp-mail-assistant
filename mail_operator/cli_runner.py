"""
LLM Provider 추상화. claude auth login / codex login 세션 기반 subprocess 호출.
API 키 불필요.
"""
import subprocess
import shutil
import logging
import json
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class LLMProvider(ABC):
    """LLM 호출 인터페이스."""

    @abstractmethod
    def analyze(
        self,
        prompt: str,
        timeout: int = 90,
        schema: Optional[dict] = None,
    ) -> dict:
        """
        Returns: {"success": bool, "content": str, "provider": str, "error": str|None}
        """


class CliProvider(LLMProvider):
    """Claude CLI 또는 Codex CLI subprocess 기반 LLM Provider."""

    def __init__(
        self,
        provider_name: Optional[str] = None,
        allow_fallback: Optional[bool] = None,
        model: Optional[str] = None,
    ):
        self.provider_name = (provider_name or os.environ.get("LLM_PROVIDER", "claude")).lower()
        if self.provider_name not in {"claude", "codex", "auto"}:
            raise ValueError("LLM_PROVIDER는 claude, codex, auto 중 하나여야 합니다")
        if allow_fallback is None:
            allow_fallback = os.environ.get("LLM_ALLOW_FALLBACK", "false").lower() == "true"
        self.allow_fallback = allow_fallback
        self.model = model or os.environ.get("LLM_MODEL") or os.environ.get("OPERATOR_MODEL")
        self.cli = self._find_cli(self.provider_name)

    @staticmethod
    def _find_cli(provider_name: str = "auto") -> Optional[str]:
        names = [provider_name] if provider_name in {"claude", "codex"} else ["claude", "codex"]
        for name in names:
            path = shutil.which(name)
            if path:
                return path
            if sys.platform == "win32":
                for ext in [".cmd", ".ps1", ".exe"]:
                    path = shutil.which(name + ext)
                    if path:
                        return path
        return None

    def _build_cmd(
        self,
        cli_path: str,
        prompt: str,
        schema: Optional[dict] = None,
    ) -> list:
        name = cli_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower()
        if "claude" in name:
            cmd = [
                cli_path,
                "-p",
                prompt,
                "--output-format",
                "json" if schema else "text",
                "--tools",
                "",
                "--no-session-persistence",
            ]
            if schema:
                cmd.extend(["--json-schema", json.dumps(schema, ensure_ascii=False)])
            if getattr(self, "model", None):
                cmd.extend(["--model", self.model])
            return cmd
        cmd = [
            cli_path,
            "exec",
            "--sandbox",
            "read-only",
            "--ephemeral",
            prompt,
        ]
        if getattr(self, "model", None):
            cmd[2:2] = ["--model", self.model]
        return cmd

    @staticmethod
    def _run_subprocess(cmd: list, timeout: int) -> subprocess.CompletedProcess:
        use_shell = sys.platform == "win32" and str(cmd[0]).endswith(".cmd")
        return subprocess.run(
            cmd if not use_shell else " ".join(f'"{c}"' if " " in c else c for c in cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=use_shell,
        )

    @staticmethod
    def _provider_label(cli_path: str) -> str:
        return Path(cli_path).stem.lower()

    @staticmethod
    def _structured_content(raw: str) -> str:
        """Claude ``--output-format json`` envelope에서 구조화 결과를 꺼냅니다."""
        envelope = json.loads(raw)
        if not isinstance(envelope, dict):
            raise ValueError("LLM JSON envelope가 객체가 아닙니다")
        if envelope.get("is_error"):
            raise RuntimeError(str(envelope.get("result") or "LLM structured output failed"))
        structured = envelope.get("structured_output")
        if structured is not None:
            return json.dumps(structured, ensure_ascii=False)
        result = envelope.get("result")
        if isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False)
        if isinstance(result, str):
            return result
        raise ValueError("LLM JSON envelope에 structured_output/result가 없습니다")

    @staticmethod
    def _failure_message(result: subprocess.CompletedProcess) -> str:
        """CLI별 실패 채널(stderr/stdout/JSON envelope)을 한 문자열로 정규화합니다."""
        stderr = (result.stderr or "").strip()
        if stderr:
            return stderr[:1000]
        stdout = (result.stdout or "").strip()
        if stdout:
            try:
                envelope = json.loads(stdout)
                if isinstance(envelope, dict) and envelope.get("result"):
                    return str(envelope["result"])[:1000]
            except (TypeError, ValueError):
                pass
            return stdout[:1000]
        return f"rc={result.returncode}"

    def analyze(
        self,
        prompt: str,
        timeout: int = 90,
        schema: Optional[dict] = None,
    ) -> dict:
        if not self.cli:
            raise RuntimeError(
                "LLM CLI를 찾을 수 없습니다. "
                "'claude auth login' 또는 'codex login'으로 먼저 로그인하세요."
            )

        cmd = self._build_cmd(self.cli, prompt, schema=schema)
        logger.info(f"[CliProvider] {cmd[0]} subprocess 호출")

        result = self._run_subprocess(cmd, timeout)

        if result.returncode != 0:
            if getattr(self, "allow_fallback", False) and "claude" in cmd[0].lower():
                codex = shutil.which("codex") or shutil.which("codex.cmd")
                if codex:
                    logger.warning("[CliProvider] 명시적으로 허용된 codex fallback")
                    fb = self._run_subprocess(self._build_cmd(codex, prompt, schema=schema), timeout)
                    if fb.returncode == 0:
                        return {"success": True, "content": fb.stdout.strip(),
                                "provider": "codex", "error": None}
            return {"success": False, "content": "", "provider": self._provider_label(cmd[0]),
                    "error": self._failure_message(result)}

        try:
            content = (
                self._structured_content(result.stdout.strip())
                if schema and "claude" in cmd[0].lower()
                else result.stdout.strip()
            )
        except Exception as exc:
            return {
                "success": False,
                "content": "",
                "provider": self._provider_label(cmd[0]),
                "error": f"구조화 출력 파싱 실패: {exc}",
            }

        return {"success": True, "content": content,
                "provider": self._provider_label(cmd[0]), "error": None}


def parse_llm_json(raw: str) -> dict:
    """LLM 응답에서 JSON 추출. 코드펜스 제거 후 JSONDecoder.raw_decode 사용."""
    import json
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    decoder = json.JSONDecoder()
    start = text.find("{")
    if start == -1:
        raise ValueError(f"JSON 객체를 찾을 수 없음: {text[:200]}")
    obj, _ = decoder.raw_decode(text, start)
    return obj


_default_provider: Optional[CliProvider] = None


def configure_provider(
    provider_name: str,
    allow_fallback: bool = False,
    model: Optional[str] = None,
) -> CliProvider:
    """Operator 설정으로 프로세스 전역 provider를 명시적으로 고정합니다."""
    global _default_provider
    _default_provider = CliProvider(
        provider_name=provider_name,
        allow_fallback=allow_fallback,
        model=model,
    )
    return _default_provider


def get_provider() -> CliProvider:
    global _default_provider
    if _default_provider is None:
        _default_provider = CliProvider()
    return _default_provider


def run_llm(
    prompt: str,
    timeout: int = 90,
    schema: Optional[dict] = None,
) -> dict:
    """편의 함수: get_provider().analyze() 위임."""
    return get_provider().analyze(prompt, timeout=timeout, schema=schema)
