"""Runs every fake in one process: supervisor on :80, Strava :8001, Anthropic :8002, Zwift :8003, Garmin :8004,
MyWhoosh :8005."""
import asyncio

import anthropic_fake
import garmin
import mywhoosh
import strava
import supervisor
import uvicorn
import zwift


async def main():
    servers = [uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning"))
               for app, port in ((supervisor.app, 80), (strava.app, 8001), (anthropic_fake.app, 8002), (zwift.app, 8003),
                                 (garmin.app, 8004), (mywhoosh.app, 8005))]
    await asyncio.gather(*(s.serve() for s in servers))


asyncio.run(main())
