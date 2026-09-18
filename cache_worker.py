#!/usr/bin/env python3
"""A single upload attempt, isolated from the API and scheduler."""
import sys
import cache_server

if __name__ == '__main__':
    site, code = sys.argv[1:3]
    job = cache_server.get_job(site, code) or {}
    cache_server.run_worker(site, code, job.get('title', ''))
