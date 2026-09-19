"""Local, live-reading dashboard for this bot's eval-runs/ and run-logs/.

Run with:  uv run python scripts/dashboard/serve.py [--port 8787]

Every request re-reads the log files from disk, so re-running `make evals`
or `make run-webrtc` and refreshing the page is all it takes to see new data
-- nothing to regenerate by hand. Each teammate runs this against their own
checkout to explore their own runs.
"""
import argparse
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from eval_parser import list_eval_runs, parse_eval_run
from real_parser import parse_real_calls

SERVER_DIR = Path(__file__).resolve().parents[2]
EVAL_RUNS_DIR = SERVER_DIR / "eval-runs"
RUN_LOGS_DIR = SERVER_DIR / "run-logs"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI()


@app.get("/api/eval-runs")
def api_list_eval_runs():
    return list_eval_runs(EVAL_RUNS_DIR)


@app.get("/api/eval-runs/{run_id}")
def api_get_eval_run(run_id: str):
    run_dir = EVAL_RUNS_DIR / run_id
    if not run_dir.is_dir():
        return JSONResponse({"error": f"no such run: {run_id}"}, status_code=404)
    return parse_eval_run(run_dir)


@app.get("/api/real-calls")
def api_real_calls():
    return parse_real_calls(RUN_LOGS_DIR)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main():
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    print(f"eval-runs dir: {EVAL_RUNS_DIR}")
    print(f"run-logs dir:  {RUN_LOGS_DIR}")
    print(f"dashboard:     http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
