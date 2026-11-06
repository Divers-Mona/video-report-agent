"""Public core command-line entry points."""

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from .doctor import run_doctor
from .execution import generate
from .local_models import LocalModelError, discover_models, resolve_model_selection
from .pi import PI_THINKING_LEVELS, PROJECT_ROOT
from .pipeline import create_run


def main():
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=Path, default=Path("runs"))
    parser.add_argument(
        "--database", type=Path, help="Shared embedding database; default runs/state.sqlite3"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("models", help="list model IDs exposed by the configured local service")
    sub.add_parser("doctor", help="check local dependencies without sending inference requests")
    for command in ("generate", "transcribe"):
        run = sub.add_parser(command)
        run.add_argument("url")
        run.add_argument("--model", help="select a model ID for this run")
        run.add_argument(
            "--provider",
            help="Pi provider for this run (overrides PI_PROVIDER; non-local requires --model)",
        )
        run.add_argument(
            "--thinking", choices=PI_THINKING_LEVELS, help="Pi thinking level for this run"
        )
        run.add_argument("--report-mode", choices=["standard", "brief"], default="standard")
        run.add_argument("--transcript-mode", choices=["auto", "asr-only", "fused"], default="auto")
        run.add_argument("--ocr-mode", choices=["off", "auto", "roi", "on"], default="off")
        run.add_argument("--ocr-roi")
        run.add_argument("--subtitle-file", type=Path)
        run.add_argument(
            "--asr-backend", choices=["mlx", "paraformer", "faster-whisper", "whisper-cpp"]
        )
        run.add_argument("--asr-model")
        run.add_argument("--asr-language")
        run.add_argument("--ocr-backend", choices=["rapidocr"])
        run.add_argument(
            "--sources",
            nargs="+",
            choices=["asr", "subtitle_track", "bilibili_ai"],
            help="Selected sources in primary-source order; OCR is deferred",
        )
    compare = sub.add_parser("compare-asr")
    compare.add_argument("--manifest", type=Path, required=True)
    subtitles = sub.add_parser("audit-subtitles")
    subtitles.add_argument("--manifest", type=Path, required=True)
    subtitles.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "models":
        try:
            model_ids = discover_models()
        except LocalModelError as exc:
            parser.error(str(exc))
        if model_ids:
            for model_id in model_ids:
                print(model_id)
        else:
            print("No model IDs returned by the configured service.")
        return 0
    if args.command == "doctor":
        rows = run_doctor()
        for name, ok, detail in rows:
            state = "OK" if ok is True else "SKIP" if ok is None else "FAIL"
            print(f"[{state}] {name}: {detail}")
        return 1 if any(ok is False for _, ok, _ in rows) else 0
    if args.command == "audit-subtitles":
        from .compare_subtitles import audit_subtitles

        print(json.dumps(audit_subtitles(args.manifest, args.output), ensure_ascii=False, indent=2))
        return 0
    if args.command == "compare-asr":
        from .compare_asr import compare_asr

        output = compare_asr(args.manifest, args.runs)
        print(output)
        return 0 if json.loads((output / "status.json").read_text())["state"] == "COMPLETE" else 1
    if args.command in {"generate", "transcribe"}:
        model_selection = None
        if args.command == "generate":
            try:
                model_selection = resolve_model_selection(
                    args.model, provider_override=args.provider
                )
            except LocalModelError as exc:
                parser.error(str(exc))
            if args.thinking:
                model_selection = {**(model_selection or {}), "thinking": args.thinking}
        status = generate(
            create_run(
                args.runs,
                args.url,
                report_mode=args.report_mode,
                transcript_mode=args.transcript_mode,
                transcript_sources=args.sources,
                transcript_only=args.command == "transcribe",
                ocr_mode=args.ocr_mode,
                ocr_roi=args.ocr_roi,
                subtitle_file=args.subtitle_file,
                model_selection=model_selection,
                asr_backend=args.asr_backend,
                asr_model=args.asr_model,
                asr_language=args.asr_language,
                ocr_backend=args.ocr_backend,
                database_path=args.database,
            ),
            database_path=args.database,
        )
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status["state"] in {"RENDERED", "TRANSCRIPT_READY"} else 1
