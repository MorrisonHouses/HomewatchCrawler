"""HomeWatch Railway worker.

Polls the Laravel application for queued crawls, runs the Playwright crawler,
and posts the staged results back for review.
"""
import os
import time

import requests

from crawler import crawl


APP_URL = os.environ["HOMEWATCH_APP_URL"].rstrip("/")
TOKEN = os.environ["HOMEWATCH_CRAWLER_TOKEN"]
POLL_SECONDS = max(5, int(os.getenv("HOMEWATCH_POLL_SECONDS", "20")))
REQUEST_TIMEOUT = max(15, int(os.getenv("HOMEWATCH_REQUEST_TIMEOUT", "120")))
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}


def request(method, path, **kwargs):
    return requests.request(
        method,
        f"{APP_URL}{path}",
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )


def process(job):
    job_id = job["id"]
    source = job["source"]
    print(f"CRAWL: starting job {job_id} for {source.get('builder')} — {source.get('url')}", flush=True)
    try:
        items, meta = crawl(source)
        response = request("POST", f"/api/crawler/jobs/{job_id}/complete", json={"items": items, "meta": meta})
        response.raise_for_status()
        print(f"CRAWL: completed job {job_id}; staged {len(items)} homes", flush=True)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(f"CRAWL ERROR: job {job_id}: {message}", flush=True)
        try:
            response = request("POST", f"/api/crawler/jobs/{job_id}/fail", json={"error": message[:10000]})
            response.raise_for_status()
        except Exception as report_error:
            print(f"CRAWL ERROR: could not report job {job_id}: {report_error}", flush=True)


def main():
    print(f"HomeWatch worker online; polling {APP_URL} every {POLL_SECONDS}s", flush=True)
    while True:
        try:
            response = request("GET", "/api/crawler/jobs/next")
            response.raise_for_status()
            job = response.json().get("job")
            if job:
                process(job)
            else:
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"WORKER ERROR: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
