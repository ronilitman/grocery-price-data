"""Geocode the branches OSM could not find, via Google Geocoding (KAN-34).

Run by hand, once. OSM simply does not carry most Israeli streets outside the
big cities - `חפץ חיים 4, נתניה` is absent from it at any spelling - so no
amount of cleaning our address strings recovers them. Google has the data.

The owner enabled billing for this and set a hard ceiling of 1,000 requests.
MAX_REQUESTS enforces it in the loop: the run aborts rather than spending past
it, whatever the input size. State is written after every call so a re-run
resumes instead of paying twice for the same address.
"""
import json, os, sys, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import geocode_branches as g

MAX_REQUESTS = 1000          # hard ceiling, set by the owner. Never raise silently.
DELAY = 0.2
URL = "https://maps.googleapis.com/maps/api/geocode/json"
STATE = "outputs/google_geocode_state.json"
DUMPS = "/private/tmp/claude-501/-Users-ronilitman-Documents-grocery-list-app/2b17d803-2610-4e75-9e11-0ba97ad9ba48/scratchpad/dumps"


def load(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    key = open("/tmp/gk.txt", encoding="utf-8").read().strip()
    recs = g.extract_branch_records(DUMPS)
    loc = json.load(open("data/branch_locations.json", encoding="utf-8"))["locations"]
    state = load(STATE, {})

    todo = [u for u, v in recs.items()
            if loc.get(u) is None and g.has_address(v.get("address")) and u not in state]
    print(f"to geocode: {len(todo)} (already done {len(state)}, ceiling {MAX_REQUESTS})")
    if len(state) + len(todo) > MAX_REQUESTS:
        sys.exit(f"ABORT: {len(state)+len(todo)} exceeds the {MAX_REQUESTS} ceiling.")

    spent = len(state)
    for i, uid in enumerate(todo, 1):
        if spent >= MAX_REQUESTS:
            print(f"STOPPING at the {MAX_REQUESTS}-request ceiling.")
            break
        v = recs[uid]
        parts = [v["address"]] + ([v["city_name"]] if v.get("city_name") else []) + ["ישראל"]
        q = ", ".join(parts)
        url = f"{URL}?{urllib.parse.urlencode({'address': q, 'key': key, 'language': 'iw'})}"
        try:
            d = json.load(urllib.request.urlopen(url, timeout=25))
        except Exception as e:
            state[uid] = {"status": "ERROR", "error": str(e)[:120]}
            spent += 1; time.sleep(DELAY); continue
        spent += 1
        st = d.get("status")
        if st == "OK" and d.get("results"):
            r = d["results"][0]
            state[uid] = {"status": "OK", "lat": r["geometry"]["location"]["lat"],
                          "lon": r["geometry"]["location"]["lng"],
                          "precision": r["geometry"].get("location_type"),
                          "formatted": r.get("formatted_address"), "query": q}
        else:
            state[uid] = {"status": st or "NO_RESULT", "query": q}
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        if i % 50 == 0:
            ok = sum(1 for x in state.values() if x.get("status") == "OK")
            print(f"  {i}/{len(todo)}  ok={ok}  spent={spent}")
        time.sleep(DELAY)

    ok = sum(1 for x in state.values() if x.get("status") == "OK")
    print(f"DONE. requests spent: {spent} (ceiling {MAX_REQUESTS}). resolved: {ok}")


if __name__ == "__main__":
    main()
