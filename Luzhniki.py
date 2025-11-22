# bot.py
import os
import json
import asyncio
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# -------------------------------- КОНФИГ --------------------------------

load_dotenv()

# ЛИБО через .env:
# DISCORD_TOKEN=...
# FOOTBALL_DATA_TOKEN=...
# FOOTBALL_DATA_TOKEN = os.getenv("FOOTBALL_DATA_TOKEN")

# Если хочешь временно жёстко вписать токены — раскомментируй строки ниже,
# но обязательно потом ПЕРЕСОЗДАЙ их и убери из кода.
DISCORD_TOKEN = ""
FOOTBALL_DATA_TOKEN = ""

GUILD_ID = 1225075859333845154          # ID сервера
TEXT_CHANNEL_ID = 1299347859828903977   # ID текстового канала
VOICE_CHANNEL_ID = 1289694911234310155  # ID голосового канала

FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"  # v4 API [web:54]

# Отслеживаемые турниры (доступные в free-плане) [web:57][web:56]
COMPETITIONS_TRACKED: Dict[str, str] = {
    "WC":  "FIFA World Cup",
    "CL":  "UEFA Champions League",
    "BL1": "Bundesliga",
    "DED": "Eredivisie",
    "BSA": "Campeonato Brasileiro Série A",
    "PD":  "Primera Division",
    "FL1": "Ligue 1",
    "ELC": "Championship",
    "PPL": "Primeira Liga",
    "EC":  "European Championship",
    "SA":  "Serie A",
    "PL":  "Premier League",
}

SUBSCRIPTIONS_FILE = Path("subscriptions.json")

SOUNDS = {
    "command":     "sounds/command.mp3",
    "goal":        "sounds/goal.mp3",
    "match_start": "sounds/start.mp3",
    "timeout":     "sounds/timeout.mp3",
    "match_end":   "sounds/end.mp3",
}

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

last_fixtures_state: Dict[int, Dict[str, Any]] = {}
TEAMS_CACHE: Dict[str, Dict[str, Any]] = {}
TEAMS_CACHE_BUILT = False


# ---------------------------- УТИЛИТЫ JSON-БД ----------------------------

def load_subscriptions() -> Dict[str, Any]:
    if not SUBSCRIPTIONS_FILE.exists():
        return {"users": {}}
    with SUBSCRIPTIONS_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_subscriptions(data: Dict[str, Any]) -> None:
    SUBSCRIPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with SUBSCRIPTIONS_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_team_subscription(user_id: int, team_id: int, team_name: str, league_name: str) -> None:
    db = load_subscriptions()
    users = db.setdefault("users", {})
    user_entry = users.setdefault(str(user_id), {"teams": []})

    for t in user_entry["teams"]:
        if t["team_id"] == team_id:
            return

    user_entry["teams"].append(
        {"team_id": team_id, "team_name": team_name, "league": league_name}
    )
    save_subscriptions(db)


def remove_team_subscription(user_id: int, team_id: int) -> bool:
    db = load_subscriptions()
    users = db.setdefault("users", {})
    entry = users.get(str(user_id))
    if not entry:
        return False

    before = len(entry["teams"])
    entry["teams"] = [t for t in entry["teams"] if t["team_id"] != team_id]
    changed = len(entry["teams"]) != before
    if changed:
        save_subscriptions(db)
    return changed


def clear_user_subscriptions(user_id: int) -> None:
    db = load_subscriptions()
    users = db.setdefault("users", {})
    users[str(user_id)] = {"teams": []}
    save_subscriptions(db)


def get_user_subscriptions(user_id: int) -> List[Dict[str, Any]]:
    db = load_subscriptions()
    return db.get("users", {}).get(str(user_id), {}).get("teams", [])


# ---------------------------- УТИЛИТЫ ВРЕМЕНИ ----------------------------

def format_match_time(utc_iso: str) -> str:
    """
    Конвертирует ISO-время UTC (2025-11-24T20:00:00Z)
    во время по Москве (UTC+3) и форматирует как ДД.ММ.ГГГГ ЧЧ:ММ (по МСК).
    """
    try:
        # парсим время как UTC
        dt_utc = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))

        # Москва всегда UTC+3, без летнего времени. [web:109]
        dt_msk = dt_utc + timedelta(hours=3)

        return dt_msk.strftime("%d.%m.%Y %H:%M") + " (по МСК)"
    except Exception:
        return utc_iso


# ------------------------- РАБОТА С football-data.org --------------------

def football_headers() -> Dict[str, str]:
    return {
        "X-Auth-Token": FOOTBALL_DATA_TOKEN or "",
        "Accept": "application/json",
    }  # [web:54][web:53]


async def build_teams_cache(session: aiohttp.ClientSession):
    """
    Загружает список команд по всем отслеживаемым турнирам и кладёт в кэш.
    Учитывает лимит free-плана (10 запросов в минуту). [web:26][web:84]
    """
    global TEAMS_CACHE, TEAMS_CACHE_BUILT

    if TEAMS_CACHE_BUILT:
        return

    for idx, (code, league_name) in enumerate(COMPETITIONS_TRACKED.items(), start=1):
        url = f"{FOOTBALL_DATA_BASE}/competitions/{code}/teams"
        async with session.get(url, headers=football_headers()) as resp:
            data = await resp.json()

        if resp.status == 429:
            print(f"[teams_cache] Рейтлимит 429 для {code}: {data}")
            break

        if resp.status != 200:
            print(f"[teams_cache] Ошибка {resp.status} для {code}: {data}")
            continue

        for t in data.get("teams", []):
            name = t.get("name")
            if not name:
                continue
            key = name.lower()
            TEAMS_CACHE[key] = {
                "team_id": t["id"],
                "team_name": name,
                "league_code": code,
                "league_name": league_name,
            }

        # Пауза ~7 секунд между запросами, чтобы не вылетать по лимиту 10 req/min [web:84]
        if idx < len(COMPETITIONS_TRACKED):
            await asyncio.sleep(7)

    print(f"[teams_cache] Загружено команд: {len(TEAMS_CACHE)}")
    TEAMS_CACHE_BUILT = True


async def search_team(session: aiohttp.ClientSession, query: str) -> Optional[Dict[str, Any]]:
    """
    Ищет команду по названию среди команд отслеживаемых турниров. [web:54]
    """
    await build_teams_cache(session)

    if not TEAMS_CACHE:
        print("[search_team] TEAMS_CACHE пуст — проверь токен и доступные лиги в кабинете football-data.org")
        return None

    q = query.lower().strip()

    # 1) точное совпадение
    if q in TEAMS_CACHE:
        return TEAMS_CACHE[q]

    # 2) совпадение по началу
    for key, info in TEAMS_CACHE.items():
        if key.startswith(q):
            return info

    # 3) совпадение по подстроке
    for key, info in TEAMS_CACHE.items():
        if q in key:
            return info

    return None


async def fetch_live_fixtures(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    """
    Live-матчи по всем отслеживаемым турнирам.
    /v4/competitions/{code}/matches?status=LIVE [web:51][web:54]
    """
    fixtures: List[Dict[str, Any]] = []
    for code in COMPETITIONS_TRACKED.keys():
        url = f"{FOOTBALL_DATA_BASE}/competitions/{code}/matches"
        params = {"status": "LIVE"}
        async with session.get(url, params=params, headers=football_headers()) as resp:
            data = await resp.json()
        fixtures.extend(data.get("matches", []))
    return fixtures


async def fetch_upcoming_fixtures_for_channel(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    """
    Ближайшие SCHEDULED-матчи по отслеживаемым турнирам за 3 дня. [web:54]
    """
    fixtures: List[Dict[str, Any]] = []
    today = datetime.now(timezone.utc).date()
    date_from = today.isoformat()
    date_to = (today + timedelta(days=3)).isoformat()

    for code in COMPETITIONS_TRACKED.keys():
        url = f"{FOOTBALL_DATA_BASE}/competitions/{code}/matches"
        params = {
            "status": "SCHEDULED",
            "dateFrom": date_from,
            "dateTo": date_to,
        }
        async with session.get(url, params=params, headers=football_headers()) as resp:
            data = await resp.json()
        fixtures.extend(data.get("matches", []))

    fixtures.sort(key=lambda m: m.get("utcDate", ""))
    return fixtures


def normalize_league_input(league_name: str) -> Optional[str]:
    """
    Приводит ввод пользователя к коду турнира (PL / CL / EC / WC / ...). [web:57]
    """
    text = league_name.strip().lower()

    aliases = {
        "apl": "PL", "апл": "PL", "premier league": "PL", "epl": "PL",
        "ла лига": "PD", "laliga": "PD", "la liga": "PD",
        "серия а": "SA", "serie a": "SA",
        "bundesliga": "BL1", "бундеслига": "BL1",
        "ligue 1": "FL1", "лига 1": "FL1",
        "championship": "ELC",
        "primeira liga": "PPL", "примейра лига": "PPL",
        "uefa champions league": "CL", "champions league": "CL", "лига чемпионов": "CL",
        "world cup": "WC", "fifa world cup": "WC", "чм": "WC",
        "euro": "EC", "european championship": "EC", "чемпионат европы": "EC",
        "brasileirao": "BSA", "серия а бразилия": "BSA",
    }
    if text in aliases:
        return aliases[text]

    if text.upper() in COMPETITIONS_TRACKED:
        return text.upper()

    for code, name in COMPETITIONS_TRACKED.items():
        if text in name.lower():
            return code

    return None


async def fetch_league_table(session: aiohttp.ClientSession, league_name: str) -> Optional[Dict[str, Any]]:
    """
    /v4/competitions/{code}/standings [web:55]
    """
    code = normalize_league_input(league_name)
    if not code:
        return None

    url = f"{FOOTBALL_DATA_BASE}/competitions/{code}/standings"
    async with session.get(url, headers=football_headers()) as resp:
        data = await resp.json()
    if "standings" not in data:
        return None
    return data


async def fetch_league_streaks(session: aiohttp.ClientSession, league_name: str) -> Optional[List[Dict[str, Any]]]:
    """
    Заглушка для /league-streaks (можно собрать из поля 'form' в standings). [web:55][web:68]
    """
    return None


# ----------------------------- ВОЙС И ЗВУК -------------------------------

async def ensure_voice_connected():
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(VOICE_CHANNEL_ID)
    if not isinstance(channel, discord.VoiceChannel):
        return

    if guild.voice_client is None or not guild.voice_client.is_connected():
        await channel.connect()
    elif guild.voice_client.channel.id != VOICE_CHANNEL_ID:
        await guild.voice_client.move_to(channel)


async def play_sound(kind: str):
    await ensure_voice_connected()
    guild = bot.get_guild(GUILD_ID)
    if not guild or guild.voice_client is None:
        return

    path = SOUNDS.get(kind)
    if not path or not Path(path).exists():
        return

    vc = guild.voice_client
    if vc.is_playing():
        vc.stop()

    source = discord.FFmpegPCMAudio(path)
    vc.play(source)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.id != bot.user.id:
        return

    if after.channel is None or (after.channel and after.channel.id != VOICE_CHANNEL_ID):
        await asyncio.sleep(1)
        await ensure_voice_connected()


# --------- ДЕКОРАТОР ДЛЯ ОГРАНИЧЕНИЯ КОМАНД ПО КАНАЛУ ---------

def only_in_allowed_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Команды бота можно использовать только на сервере, не в личных сообщениях.",
                ephemeral=True
            )
            return False
        if interaction.channel_id != TEXT_CHANNEL_ID:
            await interaction.response.send_message(
                "Эти команды разрешено использовать только в указанном служебном канале.",
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


# -------------------------- AUTOCOMPLETE ДЛЯ /live -----------------------

async def team_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    """
    Autocomplete по названию команды: ИСПОЛЬЗУЕТ только локальный TEAMS_CACHE,
    без доп. запросов к API (чтобы не ловить 429). [web:54]
    """
    choices: List[app_commands.Choice[str]] = []

    if not TEAMS_CACHE:
        # Кэш ещё не построен — пока не подсказываем, чтобы не бить API.
        return choices

    names_seen = set()
    q = current.lower().strip()

    for info in TEAMS_CACHE.values():
        name = info["team_name"]
        lname = name.lower()
        if q and q not in lname:
            continue
        if name in names_seen:
            continue
        names_seen.add(name)
        choices.append(app_commands.Choice(name=name, value=name))
        if len(choices) >= 25:
            break

    if not q and not choices:
        for info in list(TEAMS_CACHE.values())[:25]:
            name = info["team_name"]
            choices.append(app_commands.Choice(name=name, value=name))

    return choices


# ------------------------------- КОМАНДЫ -------------------------------

@tree.command(name="help", description="Показать список команд футбольного бота")
@only_in_allowed_channel()
async def help_command(interaction: discord.Interaction):
    await play_sound("command")

    embed = discord.Embed(
        title="⚽ Футбольный бот — помощь",
        description="Бот показывает live-счёт, таблицы и уведомляет о матчах отслеживаемых турниров.",
        colour=discord.Colour.blue()
    )
    embed.add_field(
        name="/live [команда]",
        value="Подписаться на уведомления по выбранной команде (поддерживаемые турниры см. /leagues).",
        inline=False
    )
    embed.add_field(
        name="/live-stop [team_id]",
        value="Отменить подписку на команду по её ID (см. /live-list).",
        inline=False
    )
    embed.add_field(
        name="/live-stop-all",
        value="Снять все твои подписки на команды.",
        inline=False
    )
    embed.add_field(
        name="/live-list",
        value="Показать список твоих подписанных команд.",
        inline=False
    )
    embed.add_field(
        name="/live-upcoming",
        value="Показать ближайшие матчи по поддерживаемым турнирам.",
        inline=False
    )
    embed.add_field(
        name="/live-now",
        value="Показать матчи, которые сейчас идут в поддерживаемых турнирах.",
        inline=False
    )
    embed.add_field(
        name="/league-table [лига]",
        value="Показать турнирную таблицу турнира (PL, CL, EC, WC, SA, BL1 и т.д.).",
        inline=False
    )
    embed.add_field(
        name="/leagues",
        value="Показать список всех доступных турниров для бота.",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="leagues", description="Показать список доступных турниров")
@only_in_allowed_channel()
async def leagues_command(interaction: discord.Interaction):
    await play_sound("command")

    lines = [f"`{code}` — {name}" for code, name in COMPETITIONS_TRACKED.items()]
    embed = discord.Embed(
        title="📚 Доступные турниры",
        description="\n".join(lines),
        colour=discord.Colour.teal()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="live", description="Подписаться на команду из поддерживаемых турниров")
@only_in_allowed_channel()
@app_commands.describe(team="Название команды (можно часть названия)")
@app_commands.autocomplete(team=team_autocomplete)
async def live_subscribe(interaction: discord.Interaction, team: str):
    # чтобы не ловить Unknown interaction при долгих запросах
    await interaction.response.defer(ephemeral=True)

    await play_sound("command")

    async with aiohttp.ClientSession() as session:
        info = await search_team(session, team)

    if not info:
        await interaction.followup.send(
            "Не удалось найти такую команду среди поддерживаемых турниров. "
            "Проверь написание названия или посмотри список лиг в /leagues.",
            ephemeral=True
        )
        return

    add_team_subscription(
        user_id=interaction.user.id,
        team_id=info["team_id"],
        team_name=info["team_name"],
        league_name=info["league_name"],
    )

    embed = discord.Embed(
        title="✅ Подписка оформлена",
        description=f"Теперь ты будешь получать уведомления по команде **{info['team_name']}** "
                    f"({info['league_name']}).",
        colour=discord.Colour.green()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="live-stop", description="Отменить подписку на команду")
@only_in_allowed_channel()
@app_commands.describe(team_id="ID команды (смотри /live-list)")
async def live_stop(interaction: discord.Interaction, team_id: int):
    await play_sound("command")

    ok = remove_team_subscription(interaction.user.id, team_id)
    if not ok:
        await interaction.response.send_message(
            "У тебя нет подписки на эту команду (проверь /live-list).",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Подписка на команду с ID `{team_id}` удалена.",
        ephemeral=True
    )


@tree.command(name="live-stop-all", description="Удалить все подписки на команды")
@only_in_allowed_channel()
async def live_stop_all(interaction: discord.Interaction):
    await play_sound("command")

    clear_user_subscriptions(interaction.user.id)
    await interaction.response.send_message(
        "Все твои подписки на команды удалены.",
        ephemeral=True
    )


@tree.command(name="live-list", description="Показать твои подписанные команды")
@only_in_allowed_channel()
async def live_list(interaction: discord.Interaction):
    await play_sound("command")

    subs = get_user_subscriptions(interaction.user.id)
    if not subs:
        await interaction.response.send_message(
            "У тебя пока нет подписок на команды. Используй `/live`, чтобы подписаться.",
            ephemeral=True
        )
        return

    desc_lines = [
        f"ID: `{t['team_id']}` — **{t['team_name']}** ({t['league']})"
        for t in subs
    ]

    embed = discord.Embed(
        title="📜 Твои команды",
        description="\n".join(desc_lines),
        colour=discord.Colour.gold()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="live-upcoming", description="Ближайшие матчи по поддерживаемым турнирам")
@only_in_allowed_channel()
async def live_upcoming(interaction: discord.Interaction):
    await play_sound("command")

    async with aiohttp.ClientSession() as session:
        fixtures = await fetch_upcoming_fixtures_for_channel(session)

    if not fixtures:
        await interaction.response.send_message(
            "Сейчас нет ближайших матчей по поддерживаемым турнирам в выбранном интервале.",
            ephemeral=True
        )
        return

    lines = []
    for m in fixtures[:10]:
        league_name = m["competition"]["name"]
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        time_str = format_match_time(m["utcDate"])
        lines.append(f"**{league_name}** — {home} vs {away} ({time_str})")

    embed = discord.Embed(
        title="📅 Ближайшие матчи",
        description="\n".join(lines),
        colour=discord.Colour.blue()
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="live-now", description="Матчи, которые сейчас идут")
@only_in_allowed_channel()
async def live_now(interaction: discord.Interaction):
    await play_sound("command")

    async with aiohttp.ClientSession() as session:
        fixtures = await fetch_live_fixtures(session)

    if not fixtures:
        await interaction.response.send_message(
            "Сейчас нет идущих матчей в поддерживаемых турнирах.",
            ephemeral=True
        )
        return

    lines = []
    for m in fixtures[:10]:
        league_name = m["competition"]["name"]
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        score = m.get("score", {})
        ft = score.get("fullTime", {}) or {}
        home_goals = ft.get("home") or 0
        away_goals = ft.get("away") or 0
        status = m.get("status", "LIVE")
        lines.append(
            f"**{league_name}** — {home} {home_goals}:{away_goals} {away} ({status})"
        )

    embed = discord.Embed(
        title="📡 Сейчас в эфире",
        description="\n".join(lines),
        colour=discord.Colour.green()
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="league-table", description="Показать турнирную таблицу турнира")
@only_in_allowed_channel()
@app_commands.describe(league="Название или код турнира (PL, La Liga, CL, EC, WC и т.п.)")
async def league_table_cmd(interaction: discord.Interaction, league: str):
    await play_sound("command")

    async with aiohttp.ClientSession() as session:
        table = await fetch_league_table(session, league)

    if not table:
        await interaction.response.send_message(
            "Не удалось найти такой турнир среди поддерживаемых. Попробуй код вроде PL, CL, EC, WC и т.д.",
            ephemeral=True
        )
        return

    league_name = table["competition"]["name"]
    standings = table["standings"][0]["table"]

    lines = []
    for row in standings[:10]:
        rank = row["position"]
        team = row["team"]["name"]
        pts = row["points"]
        lines.append(f"`{rank:>2}` {team} — {pts} очков")

    embed = discord.Embed(
        title=f"🏆 Таблица — {league_name}",
        description="\n".join(lines),
        colour=discord.Colour.purple()
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="league-streaks", description="Серии команд по турниру")
@only_in_allowed_channel()
@app_commands.describe(league="Название или код турнира")
async def league_streaks_cmd(interaction: discord.Interaction, league: str):
    await play_sound("command")

    async with aiohttp.ClientSession() as session:
        streaks = await fetch_league_streaks(session, league)

    await interaction.response.send_message(
        "Пока расчёт серий не реализован. Можно будет собрать их на основе поля `form` в standings.",
        ephemeral=True
    )


# ----------------------- ФОНОВЫЙ ОПРОС LIVE-МАТЧЕЙ ----------------------

@tasks.loop(seconds=90)
async def poll_live_matches():
    """
    Опрос live-матчей: события start/goal/pause/end.
    Звуки — только если есть хотя бы один подписчик на команды матча. [web:51][web:68]
    """
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    text_channel = guild.get_channel(TEXT_CHANNEL_ID) if guild else None

    async with aiohttp.ClientSession() as session:
        fixtures = await fetch_live_fixtures(session)

    global last_fixtures_state
    current_state: Dict[int, Dict[str, Any]] = {}
    db = load_subscriptions()
    users = db.get("users", {})

    notifications: List[Dict[str, Any]] = []

    for m in fixtures:
        match_id = m["id"]
        current_state[match_id] = m

        prev = last_fixtures_state.get(match_id)

        status = m.get("status")  # SCHEDULED, IN_PLAY, PAUSED, FINISHED и т.д. [web:51][web:26]
        score = m.get("score", {})
        ft = score.get("fullTime", {}) or {}
        home_goals = ft.get("home") or 0
        away_goals = ft.get("away") or 0

        # Старт
        if prev is None and status not in ("SCHEDULED", "POSTPONED", "CANCELLED"):
            notifications.append(
                {"type": "start", "match": m, "message": "Матч начался!"}
            )

        # Гол
        if prev is not None:
            prev_score = prev.get("score", {})
            prev_ft = prev_score.get("fullTime", {}) or {}
            prev_home = prev_ft.get("home") or 0
            prev_away = prev_ft.get("away") or 0
            if home_goals != prev_home or away_goals != prev_away:
                notifications.append(
                    {"type": "goal", "match": m, "message": "Забит гол!"}
                )

        # Перерыв
        if status == "PAUSED" and (prev is None or prev.get("status") != "PAUSED"):
            notifications.append(
                {"type": "pause", "match": m, "message": "Перерыв в матче."}
            )

        # Конец
        if status == "FINISHED" and (prev is None or prev.get("status") != "FINISHED"):
            notifications.append(
                {"type": "end", "match": m, "message": "Матч окончен."}
            )

    last_fixtures_state = current_state

    for note in notifications:
        m = note["match"]
        home = m["homeTeam"]
        away = m["awayTeam"]
        league_name = m["competition"]["name"]
        score = m.get("score", {})
        ft = score.get("fullTime", {}) or {}
        home_goals = ft.get("home") or 0
        away_goals = ft.get("away") or 0

        involved_team_ids = {home["id"], away["id"]}

        matched_users: List[int] = []
        for user_id_str, entry in users.items():
            user_teams = {t["team_id"] for t in entry.get("teams", [])}
            if user_teams & involved_team_ids:
                matched_users.append(int(user_id_str))

        if not matched_users:
            continue

        if note["type"] == "goal":
            await play_sound("goal")
        elif note["type"] == "end":
            await play_sound("match_end")
        elif note["type"] == "start":
            await play_sound("match_start")
        elif note["type"] == "pause":
            await play_sound("timeout")

        text = (
            f"**{note['message']}**\n"
            f"Турнир: **{league_name}**\n"
            f"Матч: **{home['name']} {home_goals}:{away_goals} {away['name']}**"
        )

        embed = discord.Embed(
            title="⚽ Уведомление о матче",
            description=text,
            colour=discord.Colour.orange()
        )

        for user_id in matched_users:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            try:
                await user.send(embed=embed)
            except discord.Forbidden:
                pass

        if text_channel and text_channel.permissions_for(guild.me).send_messages:
            await text_channel.send(embed=embed)


# --------------------------- ЖИЗНЕННЫЙ ЦИКЛ БОТА --------------------------

@bot.event
async def on_ready():
    print(f"Вошёл как {bot.user} (ID: {bot.user.id})")
    await bot.wait_until_ready()
    await bot.change_presence(activity=discord.Game(name="Футбол (football-data.org)"))

    await ensure_voice_connected()

    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

    if not poll_live_matches.is_running():
        poll_live_matches.start()


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Не задан DISCORD_TOKEN (переменная окружения).")
    if not FOOTBALL_DATA_TOKEN:
        raise RuntimeError("Не задан FOOTBALL_DATA_TOKEN (токен football-data.org).")
    bot.run(DISCORD_TOKEN)
