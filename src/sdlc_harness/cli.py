import argparse
import json
import os
from datetime import date

from .providers import ProviderSettings, load_environment
from .store import Store


def main():
    load_environment()
    parser = argparse.ArgumentParser(description="Run the SDLC Harness POC with AWS Bedrock; loads .env from the working directory")
    parser.add_argument("--data-dir", default=os.environ.get("HARNESS_DATA_DIR", "data/projects"))
    parser.add_argument("--provider", choices=["bedrock"])
    parser.add_argument("--model-id", help="Bedrock model ID or inference profile ID/ARN")
    parser.add_argument("--region", help="Bedrock AWS region")
    parser.add_argument("--sandbox", choices=["docker"])
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    create = commands.add_parser("create")
    create.add_argument("idea")
    create.add_argument("--blueprint", default="default")
    create.add_argument("--start-date", type=date.fromisoformat)
    commands.add_parser("list")
    commands.add_parser("doctor", help="Inspect configuration and Docker locally; no AWS requests")
    commands.add_parser("check-model", help="Send one small Bedrock inference request (billable AWS usage)")
    for command in ["status", "run", "approve", "send-back", "rerun", "events", "stop"]:
        subparser = commands.add_parser(command)
        subparser.add_argument("project_id")
        if command == "send-back":
            subparser.add_argument("feedback")
        if command == "rerun":
            subparser.add_argument("stage")
    args = parser.parse_args()
    try:
        store = Store(args.data_dir)
        if args.command in {"list", "status", "events"}:
            result = ([p.model_dump(mode="json") for p in store.projects()] if args.command == "list"
                      else store.events(args.project_id) if args.command == "events" else store.load(args.project_id))
            print(result.model_dump_json(indent=2) if hasattr(result, "model_dump_json") else json.dumps(result, indent=2))
            return
        settings = ProviderSettings.from_env(provider=args.provider, model_id=args.model_id,
                                             region=args.region, sandbox=args.sandbox)
        if args.command == "doctor":
            print(json.dumps(settings.doctor(), indent=2))
            return
        if args.command == "check-model":
            if settings.provider != "bedrock":
                raise ValueError("check-model requires --provider bedrock (or HARNESS_PROVIDER=bedrock)")
            gateway = settings.gateway()
            reply = gateway.complete(role="diagnostic", task="connection_check", inputs={},
                                     prompt='Return exactly this JSON object: {"ok": true}',
                                     max_tokens=64, timeout=settings.timeout_seconds)
            try:
                valid = json.loads(reply.text) == {"ok": True} and reply.stop_reason == "end_turn"
            except ValueError:
                valid = False
            if not valid:
                raise RuntimeError("Bedrock returned an unexpected diagnostic response; connection reached the model but the JSON check failed")
            print(json.dumps({"ok": True, "provider": "bedrock", "model_id": gateway.model_id,
                              "region": gateway.region, "usage": reply.usage}, indent=2))
            return
        if args.command == "serve":
            import uvicorn
            from .api import create_app
            uvicorn.run(create_app(args.data_dir, settings=settings), host="127.0.0.1", port=args.port)
            return
        runner = settings.runner(store)
        if args.command in {"run", "approve", "send-back", "rerun"}:
            runner.gateway_for(store.load(args.project_id))
        match args.command:
            case "create":
                project = runner.create(args.idea, args.blueprint, args.start_date)
                result = runner.run(project.id)
            case "run":
                result = runner.run(args.project_id)
            case "approve":
                runner.approve(args.project_id)
                result = runner.run(args.project_id)
            case "send-back":
                runner.send_back(args.project_id, args.feedback)
                result = runner.run(args.project_id)
            case "rerun":
                runner.rerun(args.project_id, args.stage)
                result = runner.run(args.project_id)
            case "stop":
                result = runner.stop_deployment(args.project_id)
        print(result.model_dump_json(indent=2) if hasattr(result, "model_dump_json") else json.dumps(result, indent=2))
        if getattr(result, "status", None) == "failed":
            raise SystemExit(1)
    except (ValueError, FileNotFoundError, RuntimeError) as error:
        parser.exit(2, f"harness: {error}\n")


if __name__ == "__main__":
    main()
