"""Headless replay: python -m demo.replay script.txt --scenario timeout
Script: one user message per line; '#' comments; a line '/restart' simulates a process restart."""
import argparse
import os
import time

from .session import OUT_DIR, SCENARIOS, Session


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("--scenario", default="happy", choices=list(SCENARIOS))
    ap.add_argument("--out", default=OUT_DIR)
    a = ap.parse_args()
    s = Session(a.scenario, a.out)
    lines = [ln.strip() for ln in open(a.script, encoding="utf-8")]
    chat = []
    for ln in lines:
        if not ln or ln.startswith("#"):
            continue
        if ln == "/restart":
            s.restart()
            chat.append("--- process restarted, agent resumed from storage ---")
            continue
        r = s.send(ln)
        chat += [f"USER : {ln}", f"AGENT: {r['agent']}  [{r['phase']}]", ""]
    s.close()
    base = os.path.join(a.out, f"transcript-{a.scenario}-{time.strftime('%H%M%S')}.txt")
    open(base, "w", encoding="utf-8").write("\n".join(chat) + "\n")
    print("\n".join(chat))
    print(f"transcript: {base}\ntrace: {s.trace_path}")


if __name__ == "__main__":
    main()
