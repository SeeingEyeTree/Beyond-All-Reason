#!/usr/bin/env python3
"""Download BAR replay files for normal-ended, human-only 1v1 games.

The script reads replay metadata from https://api.bar-rts.com/replays,
constructs replay file URLs, and downloads matching `.sdfz` files.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


API_BASE_URL = "https://api.bar-rts.com/replays"
STORAGE_BASE_URL = "https://storage.uk.cloud.ovh.net/v1/{auth}/BAR/demos"
AUTH_TOKEN = "AUTH_10286efc0d334efd917d476d7183232e"
GAME_VERSION = "2025.06.12"
OUTPUT_DIR = Path("downloads/replays")

TIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{3}$")


class ReplayFormatError(ValueError):
    """Raised when required replay metadata fields are missing."""


def get_json(url: str) -> Any:
    request = Request(url, headers={"User-Agent": "bar-replay-downloader/1.0"})
    with urlopen(request, timeout=30) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def extract_replays(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    for key in ("replays", "results", "data", "items"):
        maybe = payload.get(key)
        if isinstance(maybe, list):
            return [item for item in maybe if isinstance(item, dict)]

    return []


def has_ai(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict, str)):
        return len(value) > 0
    return bool(value)


def iter_possible_ai_fields(replay: dict[str, Any]) -> Iterable[Any]:
    for key in ("AI", "ai", "AIs", "ais", "Bots", "bots"):
        if key in replay:
            yield replay[key]

    for container_key in ("AllyTeams", "allyTeams", "teams"):
        teams = replay.get(container_key)
        if isinstance(teams, list):
            for team in teams:
                if not isinstance(team, dict):
                    continue
                for key in ("AI", "ai", "AIs", "ais", "Bots", "bots"):
                    if key in team:
                        yield team[key]


def is_human_1v1(replay: dict[str, Any]) -> bool:
    for ai_field in iter_possible_ai_fields(replay):
        if has_ai(ai_field):
            return False

    for container_key in ("AllyTeams", "allyTeams", "teams"):
        teams = replay.get(container_key)
        if isinstance(teams, list) and len(teams) == 2:
            player_counts: list[int] = []
            for team in teams:
                if not isinstance(team, dict):
                    return False
                players = team.get("Players")
                if players is None:
                    players = team.get("players")
                if not isinstance(players, list):
                    return False
                player_counts.append(len(players))
            return player_counts == [1, 1]

    players = replay.get("Players")
    if players is None:
        players = replay.get("players")
    return isinstance(players, list) and len(players) == 2


def ended_normally_with_winner(replay: dict[str, Any]) -> bool:
    winning_keys = (
        "winningAllyTeam",
        "winningAllyTeamId",
        "winningTeam",
        "winningTeamId",
        "winner",
        "winnerTeamId",
    )
    winner = next((replay.get(key) for key in winning_keys if replay.get(key) is not None), None)

    if winner is None:
        return False

    normal_flags = ("gameEndedNormally", "endedNormally", "normalEnd")
    for key in normal_flags:
        if key in replay:
            return bool(replay.get(key))

    for key in ("endReason", "terminationReason", "result"):
        value = replay.get(key)
        if isinstance(value, str) and value.lower() in {"normal", "victory", "gameover", "defeat"}:
            return True

    # Fallback: if we have a winner and no contrary indicator, treat as normal completion.
    return True


def normalize_time(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        raise ReplayFormatError("missing replay start time")

    if TIME_PATTERN.match(raw):
        return raw

    fixed = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(fixed)
    except ValueError as exc:
        raise ReplayFormatError(f"cannot parse replay time '{raw}'") from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    millis = int(dt.microsecond / 1000)
    return dt.strftime("%Y-%m-%d_%H-%M-%S-") + f"{millis:03d}"


def extract_time_and_map(replay: dict[str, Any]) -> tuple[str, str]:
    time_fields = ("startTime", "gameTime", "startedAt", "createdAt", "date")
    map_fields = ("mapName", "map", "map_name")

    raw_time = next((replay.get(key) for key in time_fields if replay.get(key)), None)
    map_name = next((replay.get(key) for key in map_fields if replay.get(key)), None)

    if map_name is None:
        raise ReplayFormatError("missing map name")

    return normalize_time(raw_time), str(map_name)


def build_download_url(replay: dict[str, Any], auth_token: str, game_version: str) -> str:
    replay_time, map_name = extract_time_and_map(replay)
    encoded_map = quote(map_name, safe="-_.()")
    filename = f"{replay_time}_{encoded_map}_{game_version}.sdfz"
    return f"{STORAGE_BASE_URL.format(auth=auth_token)}/{filename}"


def replay_id(replay: dict[str, Any]) -> str:
    for key in ("id", "replayId", "replay_id"):
        value = replay.get(key)
        if value is not None:
            return str(value)
    return "unknown"


def download_file(url: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "bar-replay-downloader/1.0"})
    with urlopen(request, timeout=60) as response:
        data = response.read()
    output_path.write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--auth-token", default=AUTH_TOKEN)
    parser.add_argument("--game-version", default=GAME_VERSION)
    parser.add_argument("--max-downloads", type=int, default=50)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    downloaded = 0
    offset = 0

    while downloaded < args.max_downloads:
        query = urlencode({"limit": args.page_size, "offset": offset})
        page_url = f"{API_BASE_URL}?{query}"

        try:
            payload = get_json(page_url)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"Failed to fetch replay list from {page_url}: {exc}")
            return 1

        replays = extract_replays(payload)
        if not replays:
            print("No more replays returned by API.")
            break

        for replay in replays:
            if downloaded >= args.max_downloads:
                break
            if not is_human_1v1(replay):
                continue
            if not ended_normally_with_winner(replay):
                continue

            try:
                url = build_download_url(replay, args.auth_token, args.game_version)
            except ReplayFormatError as exc:
                print(f"Skipping replay {replay_id(replay)}: {exc}")
                continue

            output_name = url.rsplit("/", 1)[-1]
            output_path = args.output_dir / output_name

            if args.dry_run:
                print(f"[DRY RUN] {url} -> {output_path}")
                downloaded += 1
                continue

            try:
                download_file(url, output_path)
                print(f"Downloaded replay {replay_id(replay)} -> {output_path}")
                downloaded += 1
            except HTTPError as exc:
                print(f"Failed to download {url}: HTTP {exc.code}")
            except URLError as exc:
                print(f"Failed to download {url}: {exc}")

        offset += args.page_size

    print(f"Finished. Downloaded {downloaded} replay(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
