"""A client outside the cluster that scrapes prombench's fake-webservers through Caddy's ingress (patch 0103), as part of
the testbed's workload (testbeds.Prombench): GET <url> every <interval> seconds for <duration> seconds, with
User-Agent <user agent>. Prints the count of each response status (or error) at the end, so a client that isn't
really reaching them is noticed.

Usage: fake_webserver_client.py <url> <user agent> <duration> [<interval>]
"""

import collections
import sys
import time
import urllib.error
import urllib.request


def main():
    url, agent, duration = sys.argv[1], sys.argv[2], float(sys.argv[3])
    interval = float(sys.argv[4]) if len(sys.argv) > 4 else 2
    counts = collections.Counter()
    end = time.time() + duration
    while time.time() < end:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": agent}), timeout=10) as r:
                r.read()
                counts[r.status] += 1
        except urllib.error.HTTPError as e:
            counts[e.code] += 1
        except OSError as e:
            counts[type(e).__name__] += 1
        print(f"{time.strftime('%H:%M:%S')} {dict(counts)}", flush=True)
        time.sleep(interval)
    print(f"done: {url}: {dict(counts)}", flush=True)


if __name__ == "__main__":
    main()
