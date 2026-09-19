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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from eval_parser import list_eval_runs, parse_eval_run
from real_parser import parse_real_calls

SERVER_DIR = Path(__file__).resolve().parents[2]
EVAL_RUNS_DIR = SERVER_DIR / "eval-runs"
RUN_LOGS_DIR = SERVER_DIR / "run-logs"
RECORDINGS_DIR = RUN_LOGS_DIR / "recordings"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI()


@app.middleware("http")
async def no_cache(request, call_next):
    # This is a local dev dashboard whose static files change constantly
    # between edits; browser disk caching of index.html has repeatedly served
    # stale JS/CSS after a save, masking real fixes as still-broken. Disable
    # caching outright rather than chase it with cachebusting query strings.
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


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


@app.get("/api/recordings/{filename}")
def api_recording(filename: str):
    # `filename` comes straight from a call's `recording` field (real_parser.py),
    # itself a glob match under RECORDINGS_DIR -- but treat it as untrusted input
    # from the browser anyway and strip any path components before joining.
    path = RECORDINGS_DIR / Path(filename).name
    if not path.is_file():
        return JSONResponse({"error": f"no such recording: {filename}"}, status_code=404)
    return FileResponse(path, media_type="audio/wav")


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
