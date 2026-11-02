"""Produce docs/trace_timeout_after_success.jsonl by running the real agent headlessly.

request -> choose -> yes -> book times out (provider did book) -> get_booking_status=booked -> verified success,
then a repeated 'yes'. Reviews contain an injection string, which gets flagged when options are presented.
    .venv/bin/python -m demo.make_trace
"""
import os
import shutil
import tempfile

from . import session
from .session import Session

DST = os.path.join(os.path.dirname(session.OUT_DIR), "..", "docs", "trace_timeout_after_success.jsonl")
STEPS = ["Book an electrician in Indiranagar this week", "1", "yes", "yes"]


def main():
    session.SCENARIOS["timeout_inj"] = (
        "Timeout after success + injected review",
        dict(force_book_outcomes=["timeout_booked"], injection_in_reviews=True), STEPS[0])
    tmp = tempfile.mkdtemp(prefix="errand-trace-")
    s = Session("timeout_inj", tmp)
    for t in STEPS:
        r = s.send(t)
        print(f"USER : {t}\nAGENT: {r['agent']}  [{r['phase']}]\n")
    s.close()
    dst = os.path.normpath(DST)
    shutil.copyfile(s.trace_path, dst)
    print("wrote", dst)


if __name__ == "__main__":
    main()
