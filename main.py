import os
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import json
import random
import datetime
import io
import traceback

# --- 1. SETUP & CONFIG ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Persistent storage path (Railway volume mounted at /app/data)
DATA_FILE = '/app/data/market_data.json'

def load_data():
    if not os.path.exists(DATA_FILE):
        return {
            "users": {},
            "kupidtv_usernames": {},
            "active_markets": {},
            "cooldowns": {},
            "config": {
                "disabled_commands": [],
                "command_roles": {},
                "announcement_channel_id": None,
                "bet_log_channel_id": None
            }
        }
    with open(DATA_FILE, 'r') as f:
        data = json.load(f)
    data.setdefault("kupidtv_usernames", {})
    cfg = data.setdefault("config", {})
    cfg.setdefault("announcement_channel_id", None)
    cfg.setdefault("bet_log_channel_id", None)
    cfg.setdefault("disabled_commands", [])
    cfg.setdefault("command_roles", {})
    return data

def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def is_command_allowed(interaction: discord.Interaction, cmd_name: str) -> tuple[bool, str]:
    data = load_data()
    config = data.get("config", {})
    if interaction.user.guild_permissions.administrator:
        return True, ""
    if cmd_name in config.get("disabled_commands", []):
        return False, "🚫 This command is currently disabled by the Admin."
    command_roles = config.get("command_roles", {})
    if cmd_name in command_roles:
        if command_roles[cmd_name] not in [r.id for r in interaction.user.roles]:
            return False, "⛔ You do not have the required role to use this command."
    return True, ""

async def post_bet_log(action, user, market_id, market, details, color):
    data = load_data()
    log_channel_id = data["config"].get("bet_log_channel_id")
    if not log_channel_id:
        return
    channel = bot.get_channel(log_channel_id)
    if not channel:
        return
    embed = discord.Embed(title=f"📋 Bet Activity — {action}", color=color, timestamp=datetime.datetime.utcnow())
    embed.add_field(name="User", value=user.mention, inline=True)
    embed.add_field(name="Market", value=f"`{market_id}` — {market['question']}", inline=True)
    embed.add_field(name="Details", value=details, inline=False)
    embed.set_footer(text=str(user), icon_url=user.display_avatar.url)
    await channel.send(embed=embed)

def build_market_embed(market, m_id, status="OPEN"):
    color = 0x5865F2 if status == "OPEN" else 0x57F287 if status == "RESOLVED" else 0xED4245
    title = "📈 MARKET OPEN" if status == "OPEN" else "🏁 MARKET RESOLVED"
    options = market["options"]
    options_text = "\n".join(f"**{k}.** {v['label']} — {v['pool']} $KUPID" for k, v in options.items())
    embed = discord.Embed(title=title, description=f"**{market['question']}**", color=color)
    embed.add_field(name="Market ID", value=f"`{m_id}`", inline=True)
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="Options", value=options_text, inline=False)
    if status == "OPEN":
        choices = " or ".join(f"`{k}`" for k in options.keys())
        embed.add_field(name="How to Bet", value=f"Use `/bet` and enter:\n> **Market ID:** `{m_id}`\n> **Amount:** your $KUPID\n> **Choice:** {choices}", inline=False)
    if status == "RESOLVED" and market.get("winner"):
        wk = market["winner"]
        embed.add_field(name="Winner", value=f"**{wk}. {options[wk]['label']}**", inline=False)
    return embed

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f'✅ Kupid Terminal Online | User: {bot.user}')

# --- 2. ECONOMY & BANKING ---
@bot.tree.command(name="balance", description="Check your $KUPID balance (or another user's)")
@app_commands.describe(member="The user to check (leave blank for yourself)")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = is_command_allowed(interaction, "balance")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)
    data = load_data()
    member = member or interaction.user
    uid = str(member.id)
    bal = data["users"].get(uid, 1000)
    await interaction.followup.send(f"💳 {member.mention}'s Portfolio: **{bal} $KUPID**", ephemeral=True)

@bot.tree.command(name="daily", description="Claim your daily 500 $KUPID stimulus")
async def daily(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = is_command_allowed(interaction, "daily")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)
    data = load_data()
    uid = str(interaction.user.id)
    now = datetime.datetime.now()
    last_claim = data["cooldowns"].get(uid)
    if last_claim:
        last_claim_dt = datetime.datetime.fromisoformat(last_claim)
        if now < last_claim_dt + datetime.timedelta(days=1):
            remaining = (last_claim_dt + datetime.timedelta(days=1)) - now
            return await interaction.followup.send(f"⏳ Cooldown: Try again in `{str(remaining).split('.')[0]}`.", ephemeral=True)
    data["users"][uid] = data["users"].get(uid, 1000) + 500
    data["cooldowns"][uid] = now.isoformat()
    save_data(data)
    await interaction.followup.send(f"🎁 **Daily Stimulus:** +500 $KUPID added to your wallet!", ephemeral=True)

@bot.tree.command(name="pay", description="Send $KUPID to another user")
@app_commands.describe(member="Who to pay", amount="Amount of $KUPID to send")
async def pay(interaction: discord.Interaction, member: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = is_command_allowed(interaction, "pay")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)
    if amount <= 0:
        return await interaction.followup.send("❌ Invalid amount.", ephemeral=True)
    data = load_data()
    sid, rid = str(interaction.user.id), str(member.id)
    s_bal = data["users"].get(sid, 1000)
    if s_bal < amount:
        return await interaction.followup.send("❌ Insufficient funds.", ephemeral=True)
    data["users"][sid] = s_bal - amount
    data["users"][rid] = data["users"].get(rid, 1000) + amount
    save_data(data)
    await interaction.followup.send(f"💸 {interaction.user.mention} paid {member.mention} **{amount} $KUPID**.", ephemeral=True)

@bot.tree.command(name="submit_username", description="Link your KupidTv username to your account")
@app_commands.describe(username="Your KupidTv username")
async def submit_username(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = is_command_allowed(interaction, "submit_username")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)
    data = load_data()
    uid = str(interaction.user.id)
    for existing_uid, existing_name in data["kupidtv_usernames"].items():
        if existing_name.lower() == username.lower() and existing_uid != uid:
            return await interaction.followup.send(f"❌ The username **{username}** is already linked to another account.", ephemeral=True)
    old_username = data["kupidtv_usernames"].get(uid)
    data["kupidtv_usernames"][uid] = username
    data["users"].setdefault(uid, 1000)
    save_data(data)
    if old_username:
        await interaction.followup.send(f"✅ KupidTv username updated: **{old_username}** → **{username}**", ephemeral=True)
    else:
        await interaction.followup.send(f"✅ KupidTv username **{username}** linked to your account!", ephemeral=True)

@bot.tree.command(name="leaderboard", description="View the top $KUPID holders")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = is_command_allowed(interaction, "leaderboard")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)
    data = load_data()
    users = data.get("users", {})
    kupidtv = data.get("kupidtv_usernames", {})
    if not users:
        return await interaction.followup.send("📭 No users found yet.", ephemeral=True)
    sorted_users = sorted(users.items(), key=lambda x: x[1], reverse=True)[:10]
    embed = discord.Embed(title="🏆 $KUPID Leaderboard", color=0xF1C40F)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, bal) in enumerate(sorted_users):
        rank = medals[i] if i < 3 else f"`#{i+1}`"
        tv_name = kupidtv.get(uid)
        if tv_name:
            name_part = f"**{tv_name}**"
        else:
            try:
                discord_user = await bot.fetch_user(int(uid))
                name_part = f"**{discord_user.name}**"
            except Exception:
                name_part = f"`{uid}`"
        lines.append(f"{rank} {name_part} — **{bal} $KUPID**")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Top {len(sorted_users)} of {len(users)} users")
    await interaction.followup.send(embed=embed, ephemeral=True)

# --- 3. PREDICTION MARKET ENGINE ---
@bot.tree.command(name="create_market", description="[Admin] Open a new prediction market")
@app_commands.describe(question="The market question", opt_a="First option", opt_b="Second option")
@app_commands.checks.has_permissions(administrator=True)
async def create_market(interaction: discord.Interaction, question: str, opt_a: str, opt_b: str):
    await interaction.response.defer()
    data = load_data()
    m_id = str(random.randint(100, 999))
    while m_id in data["active_markets"]:
        m_id = str(random.randint(100, 999))
    market = {
        "question": question,
        "options": {"1": {"label": opt_a, "pool": 0}, "2": {"label": opt_b, "pool": 0}},
        "bets": [],
        "status": "OPEN",
        "channel_id": interaction.channel_id,
        "message_id": None,
        "winner": None
    }
    data["active_markets"][m_id] = market
    save_data(data)
    embed = build_market_embed(market, m_id)
    msg = await interaction.followup.send(embed=embed)
    data["active_markets"][m_id]["message_id"] = msg.id
    save_data(data)

@bot.tree.command(name="bet", description="Place a bet on an open market")
@app_commands.describe(market_id="The market ID", amount="Amount of $KUPID to bet", choice="Option number (1 or 2)")
async def bet(interaction: discord.Interaction, market_id: str, amount: int, choice: str):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = is_command_allowed(interaction, "bet")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)
    data = load_data()
    uid = str(interaction.user.id)
    market = data["active_markets"].get(market_id)
    if not market or market["status"] != "OPEN":
        return await interaction.followup.send("❌ Market unavailable.", ephemeral=True)
    if choice not in market["options"]:
        valid = " or ".join(f"`{k}`" for k in market["options"].keys())
        return await interaction.followup.send(f"❌ Invalid choice. Pick {valid}.", ephemeral=True)
    if next((b for b in market["bets"] if b["uid"] == uid), None):
        return await interaction.followup.send("❌ You already have a bet. Use `/edit_bet` or `/remove_bet`.", ephemeral=True)
    u_bal = data["users"].get(uid, 1000)
    if amount <= 0 or amount > u_bal:
        return await interaction.followup.send("❌ Insufficient $KUPID.", ephemeral=True)
    chosen_label = market["options"][choice]["label"]
    data["users"][uid] = u_bal - amount
    market["bets"].append({"uid": uid, "amount": amount, "choice": choice, "user_name": str(interaction.user)})
    market["options"][choice]["pool"] += amount
    save_data(data)
    await interaction.followup.send(f"✅ Bet Locked: **{amount} $KUPID** on **{choice}. {chosen_label}** (Market `{market_id}`)", ephemeral=True)
    await post_bet_log("Bet Placed", interaction.user, market_id, market, f"**{amount} $KUPID** on **{choice}. {chosen_label}**", 0x5865F2)

@bot.tree.command(name="check_bet", description="Check all current bets on a market")
@app_commands.describe(market_id="The market ID to inspect")
async def check_bet(interaction: discord.Interaction, market_id: str):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = is_command_allowed(interaction, "check_bet")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)
    data = load_data()
    market = data["active_markets"].get(market_id)
    if not market:
        return await interaction.followup.send("❌ Market not found.", ephemeral=True)
    total_pot = sum(b["amount"] for b in market["bets"])
    embed = discord.Embed(title=f"🔍 Bets — Market `{market_id}`", description=f"**{market['question']}**", color=0x5865F2)
    for key, opt in market["options"].items():
        bettors = [b for b in market["bets"] if b["choice"] == key]
        lines = "\n".join(f"<@{b['uid']}> — {b['amount']} $KUPID" for b in bettors) if bettors else "_No bets yet_"
        embed.add_field(name=f"{key}. {opt['label']} — {opt['pool']} $KUPID", value=lines, inline=False)
    embed.set_footer(text=f"Total pot: {total_pot} $KUPID")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="edit_bet", description="Change your bet on an open market")
@app_commands.describe(market_id="The market ID", new_choice="New option (1 or 2)", new_amount="New amount")
async def edit_bet(interaction: discord.Interaction, market_id: str, new_choice: str, new_amount: int):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = is_command_allowed(interaction, "edit_bet")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)
    data = load_data()
    uid = str(interaction.user.id)
    market = data["active_markets"].get(market_id)
    if not market or market["status"] != "OPEN":
        return await interaction.followup.send("❌ Market unavailable.", ephemeral=True)
    if new_choice not in market["options"]:
        valid = " or ".join(f"`{k}`" for k in market["options"].keys())
        return await interaction.followup.send(f"❌ Invalid choice. Pick {valid}.", ephemeral=True)
    existing = next((b for b in market["bets"] if b["uid"] == uid), None)
    if not existing:
        return await interaction.followup.send("❌ No bet found. Use `/bet` first.", ephemeral=True)
    if new_amount <= 0:
        return await interaction.followup.send("❌ Amount must be greater than 0.", ephemeral=True)
    old_amount, old_choice = existing["amount"], existing["choice"]
    old_label, new_label = market["options"][old_choice]["label"], market["options"][new_choice]["label"]
    available = data["users"].get(uid, 1000) + old_amount
    if new_amount > available:
        return await interaction.followup.send(f"❌ Insufficient $KUPID. You have {available} available (including refund).", ephemeral=True)
    market["options"][old_choice]["pool"] -= old_amount
    market["options"][new_choice]["pool"] += new_amount
    existing["amount"] = new_amount
    existing["choice"] = new_choice
    existing["user_name"] = str(interaction.user)
    data["users"][uid] = available - new_amount
    save_data(data)
    await interaction.followup.send(f"✏️ Bet Updated: **{old_amount} $KUPID** on **{old_choice}. {old_label}** → **{new_amount} $KUPID** on **{new_choice}. {new_label}**", ephemeral=True)
    await post_bet_log("Bet Edited", interaction.user, market_id, market, f"**{old_amount}** on **{old_choice}. {old_label}** → **{new_amount}** on **{new_choice}. {new_label}**", 0xFEE75C)

@bot.tree.command(name="remove_bet", description="Cancel and refund your bet")
@app_commands.describe(market_id="The market ID")
async def remove_bet(interaction: discord.Interaction, market_id: str):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = is_command_allowed(interaction, "remove_bet")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)
    data = load_data()
    uid = str(interaction.user.id)
    market = data["active_markets"].get(market_id)
    if not market or market["status"] != "OPEN":
        return await interaction.followup.send("❌ Market unavailable.", ephemeral=True)
    existing = next((b for b in market["bets"] if b["uid"] == uid), None)
    if not existing:
        return await interaction.followup.send("❌ No bet found on this market.", ephemeral=True)
    removed_amount, removed_choice = existing["amount"], existing["choice"]
    removed_label = market["options"][removed_choice]["label"]
    market["options"][removed_choice]["pool"] -= removed_amount
    market["bets"] = [b for b in market["bets"] if b["uid"] != uid]
    data["users"][uid] = data["users"].get(uid, 0) + removed_amount
    save_data(data)
    await interaction.followup.send(f"🗑️ Bet Removed: **{removed_amount} $KUPID** refunded from **{removed_choice}. {removed_label}**.", ephemeral=True)
    await post_bet_log("Bet Removed", interaction.user, market_id, market, f"Cancelled **{removed_amount} $KUPID** on **{removed_choice}. {removed_label}** — refunded.", 0xED4245)

@bot.tree.command(name="resolve", description="[Admin] Resolve a market and pay out winners")
@app_commands.describe(market_id="The market ID", winner="Winning option (1 or 2)")
@app_commands.checks.has_permissions(administrator=True)
async def resolve(interaction: discord.Interaction, market_id: str, winner: str):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    market = data["active_markets"].get(market_id)
    if not market:
        return await interaction.followup.send("❌ Invalid market ID.", ephemeral=True)
    if winner not in market["options"]:
        valid = " or ".join(f"`{k}`" for k in market["options"].keys())
        return await interaction.followup.send(f"❌ Invalid option. Pick {valid}.", ephemeral=True)
    winner_label = market["options"][winner]["label"]
    total_pot = sum(b["amount"] for b in market["bets"])
    win_pool = market["options"][winner]["pool"]
    loser_key = next(k for k in market["options"] if k != winner)
    loser_label = market["options"][loser_key]["label"]
    winners, losers = [], []
    for b in market["bets"]:
        if b["choice"] == winner:
            payout = int((b["amount"] / win_pool) * total_pot) if win_pool > 0 else b["amount"]
            profit = payout - b["amount"]
            data["users"][b["uid"]] = data["users"].get(b["uid"], 0) + payout
            winners.append({**b, "payout": payout, "profit": profit})
        else:
            losers.append(b)
    market["status"] = "RESOLVED"
    market["winner"] = winner
    del data["active_markets"][market_id]
    save_data(data)
    try:
        channel = bot.get_channel(market["channel_id"])
        if channel and market.get("message_id"):
            original_msg = await channel.fetch_message(market["message_id"])
            await original_msg.edit(embed=build_market_embed(market, market_id, status="RESOLVED"))
    except Exception:
        pass
    ann_channel_id = data["config"].get("announcement_channel_id")
    ann_channel = bot.get_channel(ann_channel_id) if ann_channel_id else None
    if ann_channel:
        summary_embed = discord.Embed(title="🏁 Market Resolved", description=f"**{market['question']}**\nWinner: **{winner}. {winner_label}**", color=0x57F287)
        summary_embed.add_field(name="Total Pot", value=f"{total_pot} $KUPID", inline=True)
        summary_embed.add_field(name="Winners", value=str(len(winners)), inline=True)
        summary_embed.add_field(name="Losers", value=str(len(losers)), inline=True)
        if winners:
            summary_embed.add_field(name="🏆 Winners", value="\n".join(f"<@{w['uid']}> +{w['profit']} $KUPID (payout: {w['payout']})" for w in winners), inline=False)
        if losers:
            summary_embed.add_field(name="💸 Losers", value="\n".join(f"<@{l['uid']}> -{l['amount']} $KUPID" for l in losers), inline=False)
        await ann_channel.send(embed=summary_embed)
    dm_failed = []
    for w in winners:
        try:
            u = await bot.fetch_user(int(w["uid"]))
            await u.send(f"🏆 **You won!** Market `{market_id}` — **{market['question']}**\nYou bet **{w['amount']} $KUPID** on **{winner}. {winner_label}** and received **{w['payout']} $KUPID** (profit: +{w['profit']} $KUPID).")
        except Exception:
            dm_failed.append(w["uid"])
    for l in losers:
        try:
            u = await bot.fetch_user(int(l["uid"]))
            await u.send(f"💸 **You lost.** Market `{market_id}` — **{market['question']}**\nYou bet **{l['amount']} $KUPID** on **{loser_label}** and lost it all. Better luck next time.")
        except Exception:
            dm_failed.append(l["uid"])
    note = f"\n⚠️ Could not DM: {' '.join(f'<@{uid}>' for uid in dm_failed)} — DMs likely disabled." if dm_failed else ""
    await interaction.followup.send(f"✅ Market `{market_id}` resolved. Winner: **{winner}. {winner_label}**. Bettors notified via DM.{note}", ephemeral=True)

# --- 4. CONFIGURATION & EXPORT ---
@bot.tree.command(name="configure_channel", description="[Admin] Set announcement channel")
@app_commands.describe(channel="Channel for resolved market results")
@app_commands.checks.has_permissions(administrator=True)
async def configure_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    data["config"]["announcement_channel_id"] = channel.id
    save_data(data)
    await interaction.followup.send(f"✅ Announcement channel set to {channel.mention}.", ephemeral=True)

@bot.tree.command(name="configure_bet_log", description="[Admin] Set bet activity log channel")
@app_commands.describe(channel="Channel for bet logs")
@app_commands.checks.has_permissions(administrator=True)
async def configure_bet_log(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    data["config"]["bet_log_channel_id"] = channel.id
    save_data(data)
    await interaction.followup.send(f"✅ Bet log channel set to {channel.mention}.", ephemeral=True)

@bot.tree.command(name="configure_toggle", description="[Admin] Enable or disable a command")
@app_commands.describe(cmd="Command name to toggle")
@app_commands.checks.has_permissions(administrator=True)
async def configure_toggle(interaction: discord.Interaction, cmd: str):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    if cmd in data["config"]["disabled_commands"]:
        data["config"]["disabled_commands"].remove(cmd)
        await interaction.followup.send(f"✅ `{cmd}` enabled.", ephemeral=True)
    else:
        data["config"]["disabled_commands"].append(cmd)
        await interaction.followup.send(f"🚫 `{cmd}` disabled.", ephemeral=True)
    save_data(data)

@bot.tree.command(name="configure_role", description="[Admin] Restrict a command to a role")
@app_commands.describe(cmd="Command name", role="Required role")
@app_commands.checks.has_permissions(administrator=True)
async def configure_role(interaction: discord.Interaction, cmd: str, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    data["config"]["command_roles"][cmd] = role.id
    save_data(data)
    await interaction.followup.send(f"🔐 `{cmd}` now requires @{role.name}.", ephemeral=True)

@bot.tree.command(name="export_json", description="[Admin] Download JSON backup")
@app_commands.checks.has_permissions(administrator=True)
async def export_json(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with open(DATA_FILE, 'rb') as f:
        await interaction.followup.send("📂 **JSON Backup:**", file=discord.File(f, "market_data.json"), ephemeral=True)

@bot.tree.command(name="export_txt", description="[Admin] Download TXT ledger")
@app_commands.checks.has_permissions(administrator=True)
async def export_txt(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    users = data.get("users", {})
    kupidtv = data.get("kupidtv_usernames", {})
    report = f"KUPID TERMINAL - ASSET LEDGER\nGenerated: {datetime.datetime.now()}\n{'='*45}\n\n"
    for uid, bal in sorted(users.items(), key=lambda x: x[1], reverse=True):
        name = kupidtv.get(uid, f"[Discord:{uid}]")
        report += f"{name} - {bal}\n"
    with io.BytesIO(report.encode('utf-8')) as f:
        await interaction.followup.send("📄 **Text Export:**", file=discord.File(f, "kupid_ledger.txt"), ephemeral=True)

# --- 5. ADMIN TOOLS ---
@bot.tree.command(name="give", description="[Admin] Add $KUPID to a user")
@app_commands.describe(member="User to give to", amount="Amount to add")
@app_commands.checks.has_permissions(administrator=True)
async def give(interaction: discord.Interaction, member: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    uid = str(member.id)
    data["users"][uid] = data["users"].get(uid, 1000) + amount
    save_data(data)
    await interaction.followup.send(f"✅ Added **{amount} $KUPID** to {member.mention}.", ephemeral=True)

@bot.tree.command(name="take", description="[Admin] Remove $KUPID from a user")
@app_commands.describe(member="User to take from", amount="Amount to remove")
@app_commands.checks.has_permissions(administrator=True)
async def take(interaction: discord.Interaction, member: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    uid = str(member.id)
    data["users"][uid] = max(0, data["users"].get(uid, 1000) - amount)
    save_data(data)
    await interaction.followup.send(f"🚨 Deducted **{amount} $KUPID** from {member.mention}.", ephemeral=True)

# --- 6. ERROR HANDLING ---
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = "⛔ You need Administrator permissions." if isinstance(error, app_commands.MissingPermissions) else f"❌ An error occurred: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)

bot.run(TOKEN)        }
    doc.pop("_id", None)
    data = doc
    data.setdefault("kupidtv_usernames", {})
    cfg = data.setdefault("config", {})
    cfg.setdefault("announcement_channel_id", None)
    cfg.setdefault("bet_log_channel_id", None)
    cfg.setdefault("disabled_commands", [])
    cfg.setdefault("command_roles", {})
    return data

async def save_data(data):
    await collection.replace_one({"_id": "main"}, {"_id": "main", **data}, upsert=True)

async def is_command_allowed(interaction: discord.Interaction, cmd_name: str) -> tuple[bool, str]:
    data = await load_data()
    config = data.get("config", {})

    if interaction.user.guild_permissions.administrator:
        return True, ""

    disabled = config.get("disabled_commands", [])
    if cmd_name in disabled:
        return False, "🚫 This command is currently disabled by the Admin."

    command_roles = config.get("command_roles", {})
    if cmd_name in command_roles:
        required_role_id = command_roles[cmd_name]
        user_role_ids = [role.id for role in interaction.user.roles]
        if required_role_id not in user_role_ids:
            return False, "⛔ You do not have the required role to use this command."

    return True, ""

async def post_bet_log(action: str, user: discord.Member, market_id: str, market: dict, details: str, color: int):
    data = await load_data()
    log_channel_id = data["config"].get("bet_log_channel_id")
    if not log_channel_id:
        return
    channel = bot.get_channel(log_channel_id)
    if not channel:
        return
    embed = discord.Embed(
        title=f"📋 Bet Activity — {action}",
        color=color,
        timestamp=datetime.datetime.utcnow()
    )
    embed.add_field(name="User", value=user.mention, inline=True)
    embed.add_field(name="Market", value=f"`{market_id}` — {market['question']}", inline=True)
    embed.add_field(name="Details", value=details, inline=False)
    embed.set_footer(text=str(user), icon_url=user.display_avatar.url)
    await channel.send(embed=embed)

def build_market_embed(market: dict, m_id: str, status: str = "OPEN") -> discord.Embed:
    color = 0x5865F2 if status == "OPEN" else 0x57F287 if status == "RESOLVED" else 0xED4245
    title = "📈 MARKET OPEN" if status == "OPEN" else "🏁 MARKET RESOLVED"

    options = market["options"]
    options_text = "\n".join(f"**{k}.** {v['label']} — {v['pool']} $KUPID" for k, v in options.items())

    embed = discord.Embed(title=title, description=f"**{market['question']}**", color=color)
    embed.add_field(name="Market ID", value=f"`{m_id}`", inline=True)
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="Options", value=options_text, inline=False)

    if status == "OPEN":
        choices = " or ".join(f"`{k}`" for k in options.keys())
        embed.add_field(
            name="How to Bet",
            value=f"Use `/bet` and enter:\n> **Market ID:** `{m_id}`\n> **Amount:** your $KUPID\n> **Choice:** {choices}",
            inline=False
        )

    if status == "RESOLVED" and market.get("winner"):
        winner_key = market["winner"]
        winner_label = options[winner_key]["label"]
        embed.add_field(name="Winner", value=f"**{winner_key}. {winner_label}**", inline=False)

    return embed

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f'✅ Kupid Terminal Online | User: {bot.user}')

# --- 2. ECONOMY & BANKING ---
@bot.tree.command(name="balance", description="Check your $KUPID balance (or another user's)")
@app_commands.describe(member="The user to check (leave blank for yourself)")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = await is_command_allowed(interaction, "balance")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)

    data = await load_data()
    member = member or interaction.user
    uid = str(member.id)
    bal = data["users"].get(uid, 1000)
    data["users"][uid] = bal
    await save_data(data)
    await interaction.followup.send(f"💳 {member.mention}'s Portfolio: **{bal} $KUPID**", ephemeral=True)

@bot.tree.command(name="daily", description="Claim your daily 500 $KUPID stimulus")
async def daily(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = await is_command_allowed(interaction, "daily")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)

    data = await load_data()
    uid = str(interaction.user.id)
    now = datetime.datetime.now()

    last_claim = data["cooldowns"].get(uid)
    if last_claim:
        last_claim_dt = datetime.datetime.fromisoformat(last_claim)
        if now < last_claim_dt + datetime.timedelta(days=1):
            remaining = (last_claim_dt + datetime.timedelta(days=1)) - now
            return await interaction.followup.send(
                f"⏳ Cooldown: Try again in `{str(remaining).split('.')[0]}`.", ephemeral=True
            )

    data["users"][uid] = data["users"].get(uid, 1000) + 500
    data["cooldowns"][uid] = now.isoformat()
    await save_data(data)
    await interaction.followup.send(f"🎁 **Daily Stimulus:** +500 $KUPID added to your wallet!", ephemeral=True)

@bot.tree.command(name="pay", description="Send $KUPID to another user")
@app_commands.describe(member="Who to pay", amount="Amount of $KUPID to send")
async def pay(interaction: discord.Interaction, member: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = await is_command_allowed(interaction, "pay")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)

    if amount <= 0:
        return await interaction.followup.send("❌ Invalid amount.", ephemeral=True)

    data = await load_data()
    sid, rid = str(interaction.user.id), str(member.id)
    s_bal = data["users"].get(sid, 1000)

    if s_bal < amount:
        return await interaction.followup.send("❌ Insufficient funds.", ephemeral=True)

    data["users"][sid] = s_bal - amount
    data["users"][rid] = data["users"].get(rid, 1000) + amount
    await save_data(data)
    await interaction.followup.send(f"💸 {interaction.user.mention} paid {member.mention} **{amount} $KUPID**.", ephemeral=True)

@bot.tree.command(name="submit_username", description="Link your KupidTv username to your account")
@app_commands.describe(username="Your KupidTv username")
async def submit_username(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = await is_command_allowed(interaction, "submit_username")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)

    data = await load_data()
    uid = str(interaction.user.id)

    for existing_uid, existing_name in data["kupidtv_usernames"].items():
        if existing_name.lower() == username.lower() and existing_uid != uid:
            return await interaction.followup.send(
                f"❌ The username **{username}** is already linked to another account.", ephemeral=True
            )

    old_username = data["kupidtv_usernames"].get(uid)
    data["kupidtv_usernames"][uid] = username
    data["users"].setdefault(uid, 1000)
    await save_data(data)

    if old_username:
        await interaction.followup.send(
            f"✅ KupidTv username updated: **{old_username}** → **{username}**", ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"✅ KupidTv username **{username}** linked to your account!", ephemeral=True
        )

@bot.tree.command(name="leaderboard", description="View the top $KUPID holders")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = await is_command_allowed(interaction, "leaderboard")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)

    data = await load_data()
    users = data.get("users", {})
    kupidtv = data.get("kupidtv_usernames", {})

    if not users:
        return await interaction.followup.send("📭 No users found yet.", ephemeral=True)

    sorted_users = sorted(users.items(), key=lambda x: x[1], reverse=True)[:10]

    embed = discord.Embed(title="🏆 $KUPID Leaderboard", color=0xF1C40F)
    medals = ["🥇", "🥈", "🥉"]

    lines = []
    for i, (uid, bal) in enumerate(sorted_users):
        rank = medals[i] if i < 3 else f"`#{i+1}`"
        tv_name = kupidtv.get(uid)
        if tv_name:
            name_part = f"**{tv_name}**"
        else:
            try:
                discord_user = await bot.fetch_user(int(uid))
                name_part = f"**{discord_user.name}**"
            except Exception:
                name_part = f"`{uid}`"
        lines.append(f"{rank} {name_part} — **{bal} $KUPID**")

    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Top {len(sorted_users)} of {len(users)} users")
    await interaction.followup.send(embed=embed, ephemeral=True)

# --- 3. PREDICTION MARKET ENGINE ---
@bot.tree.command(name="create_market", description="[Admin] Open a new prediction market")
@app_commands.describe(question="The market question", opt_a="First option", opt_b="Second option")
@app_commands.checks.has_permissions(administrator=True)
async def create_market(interaction: discord.Interaction, question: str, opt_a: str, opt_b: str):
    await interaction.response.defer()
    data = await load_data()

    m_id = str(random.randint(100, 999))
    while m_id in data["active_markets"]:
        m_id = str(random.randint(100, 999))

    market = {
        "question": question,
        "options": {
            "1": {"label": opt_a, "pool": 0},
            "2": {"label": opt_b, "pool": 0}
        },
        "bets": [],
        "status": "OPEN",
        "channel_id": interaction.channel_id,
        "message_id": None,
        "winner": None
    }
    data["active_markets"][m_id] = market
    await save_data(data)

    embed = build_market_embed(market, m_id)
    msg = await interaction.followup.send(embed=embed)
    data["active_markets"][m_id]["message_id"] = msg.id
    await save_data(data)

@bot.tree.command(name="bet", description="Place a bet on an open market")
@app_commands.describe(market_id="The market ID", amount="Amount of $KUPID to bet", choice="Option number (1 or 2)")
async def bet(interaction: discord.Interaction, market_id: str, amount: int, choice: str):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = await is_command_allowed(interaction, "bet")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)

    data = await load_data()
    uid = str(interaction.user.id)
    market = data["active_markets"].get(market_id)

    if not market or market["status"] != "OPEN":
        return await interaction.followup.send("❌ Market unavailable.", ephemeral=True)
    if choice not in market["options"]:
        valid = " or ".join(f"`{k}`" for k in market["options"].keys())
        return await interaction.followup.send(f"❌ Invalid choice. Pick {valid}.", ephemeral=True)

    existing = next((b for b in market["bets"] if b["uid"] == uid), None)
    if existing:
        return await interaction.followup.send(
            f"❌ You already have a bet on this market. Use `/edit_bet` to change it or `/remove_bet` to cancel it.", ephemeral=True
        )

    u_bal = data["users"].get(uid, 1000)
    if amount > u_bal or amount <= 0:
        return await interaction.followup.send("❌ Insufficient $KUPID.", ephemeral=True)

    chosen_label = market["options"][choice]["label"]
    data["users"][uid] = u_bal - amount
    market["bets"].append({"uid": uid, "amount": amount, "choice": choice, "user_name": str(interaction.user)})
    market["options"][choice]["pool"] += amount
    await save_data(data)

    await interaction.followup.send(
        f"✅ Bet Locked: **{amount} $KUPID** on **{choice}. {chosen_label}** (Market `{market_id}`)", ephemeral=True
    )

    await post_bet_log(
        action="Bet Placed",
        user=interaction.user,
        market_id=market_id,
        market=market,
        details=f"**{amount} $KUPID** on **{choice}. {chosen_label}**",
        color=0x5865F2
    )

@bot.tree.command(name="check_bet", description="Check all current bets on a market")
@app_commands.describe(market_id="The market ID to inspect")
async def check_bet(interaction: discord.Interaction, market_id: str):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = await is_command_allowed(interaction, "check_bet")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)

    data = await load_data()
    market = data["active_markets"].get(market_id)

    if not market:
        return await interaction.followup.send("❌ Market not found or already resolved.", ephemeral=True)

    total_pot = sum(b["amount"] for b in market["bets"])
    embed = discord.Embed(
        title=f"🔍 Bets — Market `{market_id}`",
        description=f"**{market['question']}**",
        color=0x5865F2
    )

    for key, opt in market["options"].items():
        bettors = [b for b in market["bets"] if b["choice"] == key]
        if bettors:
            lines = "\n".join(f"<@{b['uid']}> — {b['amount']} $KUPID" for b in bettors)
        else:
            lines = "_No bets yet_"
        embed.add_field(
            name=f"{key}. {opt['label']} — {opt['pool']} $KUPID",
            value=lines,
            inline=False
        )

    embed.set_footer(text=f"Total pot: {total_pot} $KUPID")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="edit_bet", description="Change your bet on an open market")
@app_commands.describe(market_id="The market ID", new_choice="New option number (1 or 2)", new_amount="New amount of $KUPID")
async def edit_bet(interaction: discord.Interaction, market_id: str, new_choice: str, new_amount: int):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = await is_command_allowed(interaction, "edit_bet")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)

    data = await load_data()
    uid = str(interaction.user.id)
    market = data["active_markets"].get(market_id)

    if not market or market["status"] != "OPEN":
        return await interaction.followup.send("❌ Market unavailable.", ephemeral=True)
    if new_choice not in market["options"]:
        valid = " or ".join(f"`{k}`" for k in market["options"].keys())
        return await interaction.followup.send(f"❌ Invalid choice. Pick {valid}.", ephemeral=True)

    existing = next((b for b in market["bets"] if b["uid"] == uid), None)
    if not existing:
        return await interaction.followup.send("❌ You don't have a bet on this market. Use `/bet` to place one.", ephemeral=True)

    if new_amount <= 0:
        return await interaction.followup.send("❌ Amount must be greater than 0.", ephemeral=True)

    old_amount = existing["amount"]
    old_choice = existing["choice"]
    old_label = market["options"][old_choice]["label"]
    new_label = market["options"][new_choice]["label"]

    u_bal = data["users"].get(uid, 1000) + old_amount
    if new_amount > u_bal:
        return await interaction.followup.send(f"❌ Insufficient $KUPID. You have {u_bal} $KUPID available (including your refund).", ephemeral=True)

    market["options"][old_choice]["pool"] -= old_amount
    market["options"][new_choice]["pool"] += new_amount
    existing["amount"] = new_amount
    existing["choice"] = new_choice
    existing["user_name"] = str(interaction.user)
    data["users"][uid] = u_bal - new_amount
    await save_data(data)

    await interaction.followup.send(
        f"✏️ Bet Updated: **{old_amount} $KUPID** on **{old_choice}. {old_label}** → **{new_amount} $KUPID** on **{new_choice}. {new_label}**",
        ephemeral=True
    )

    await post_bet_log(
        action="Bet Edited",
        user=interaction.user,
        market_id=market_id,
        market=market,
        details=f"**{old_amount} $KUPID** on **{old_choice}. {old_label}** → **{new_amount} $KUPID** on **{new_choice}. {new_label}**",
        color=0xFEE75C
    )

@bot.tree.command(name="remove_bet", description="Cancel and refund your bet on an open market")
@app_commands.describe(market_id="The market ID")
async def remove_bet(interaction: discord.Interaction, market_id: str):
    await interaction.response.defer(ephemeral=True)
    allowed, msg = await is_command_allowed(interaction, "remove_bet")
    if not allowed:
        return await interaction.followup.send(msg, ephemeral=True)

    data = await load_data()
    uid = str(interaction.user.id)
    market = data["active_markets"].get(market_id)

    if not market or market["status"] != "OPEN":
        return await interaction.followup.send("❌ Market unavailable.", ephemeral=True)

    existing = next((b for b in market["bets"] if b["uid"] == uid), None)
    if not existing:
        return await interaction.followup.send("❌ You don't have a bet on this market.", ephemeral=True)

    removed_amount = existing["amount"]
    removed_choice = existing["choice"]
    removed_label = market["options"][removed_choice]["label"]

    market["options"][removed_choice]["pool"] -= removed_amount
    market["bets"] = [b for b in market["bets"] if b["uid"] != uid]
    data["users"][uid] = data["users"].get(uid, 0) + removed_amount
    await save_data(data)

    await interaction.followup.send(
        f"🗑️ Bet Removed: **{removed_amount} $KUPID** refunded from **{removed_choice}. {removed_label}**.", ephemeral=True
    )

    await post_bet_log(
        action="Bet Removed",
        user=interaction.user,
        market_id=market_id,
        market=market,
        details=f"Cancelled **{removed_amount} $KUPID** bet on **{removed_choice}. {removed_label}** — refunded.",
        color=0xED4245
    )

@bot.tree.command(name="resolve", description="[Admin] Resolve a market and pay out winners")
@app_commands.describe(market_id="The market ID", winner="The winning option number (1 or 2)")
@app_commands.checks.has_permissions(administrator=True)
async def resolve(interaction: discord.Interaction, market_id: str, winner: str):
    await interaction.response.defer(ephemeral=True)

    data = await load_data()
    market = data["active_markets"].get(market_id)
    if not market:
        return await interaction.followup.send("❌ Invalid market ID.", ephemeral=True)
    if winner not in market["options"]:
        valid = " or ".join(f"`{k}`" for k in market["options"].keys())
        return await interaction.followup.send(f"❌ Invalid option. Pick {valid}.", ephemeral=True)

    winner_label = market["options"][winner]["label"]
    total_pot = sum(b["amount"] for b in market["bets"])
    win_pool = market["options"][winner]["pool"]

    loser_key = next(k for k in market["options"] if k != winner)
    loser_label = market["options"][loser_key]["label"]

    winners = []
    losers = []

    for b in market["bets"]:
        if b["choice"] == winner:
            if win_pool > 0:
                payout = int((b["amount"] / win_pool) * total_pot)
            else:
                payout = b["amount"]
            profit = payout - b["amount"]
            data["users"][b["uid"]] = data["users"].get(b["uid"], 0) + payout
            winners.append({"uid": b["uid"], "amount": b["amount"], "payout": payout, "profit": profit, "user_name": b["user_name"]})
        else:
            losers.append({"uid": b["uid"], "amount": b["amount"], "user_name": b["user_name"]})

    market["status"] = "RESOLVED"
    market["winner"] = winner
    del data["active_markets"][market_id]
    await save_data(data)

    try:
        channel = bot.get_channel(market["channel_id"])
        if channel and market.get("message_id"):
            original_msg = await channel.fetch_message(market["message_id"])
            resolved_embed = build_market_embed(market, market_id, status="RESOLVED")
            await original_msg.edit(embed=resolved_embed)
    except Exception:
        pass

    ann_channel_id = data["config"].get("announcement_channel_id")
    ann_channel = bot.get_channel(ann_channel_id) if ann_channel_id else None

    if ann_channel:
        summary_embed = discord.Embed(
            title="🏁 Market Resolved",
            description=f"**{market['question']}**\nWinner: **{winner}. {winner_label}**",
            color=0x57F287
        )
        summary_embed.add_field(name="Total Pot", value=f"{total_pot} $KUPID", inline=True)
        summary_embed.add_field(name="Winners", value=str(len(winners)), inline=True)
        summary_embed.add_field(name="Losers", value=str(len(losers)), inline=True)

        if winners:
            winners_text = "\n".join(f"<@{w['uid']}> +{w['profit']} $KUPID (payout: {w['payout']})" for w in winners)
            summary_embed.add_field(name="🏆 Winners", value=winners_text, inline=False)
        if losers:
            losers_text = "\n".join(f"<@{l['uid']}> -{l['amount']} $KUPID" for l in losers)
            summary_embed.add_field(name="💸 Losers", value=losers_text, inline=False)

        await ann_channel.send(embed=summary_embed)

    dm_failed = []
    print(f"[RESOLVE] Market {market_id}: {len(winners)} winner(s), {len(losers)} loser(s)")

    for w in winners:
        try:
            user = await bot.fetch_user(int(w["uid"]))
            await user.send(
                f"🏆 **You won!** Market `{market_id}` — **{market['question']}**\n"
                f"You bet **{w['amount']} $KUPID** on **{winner}. {winner_label}** and received **{w['payout']} $KUPID** (profit: +{w['profit']} $KUPID)."
            )
        except Exception as e:
            print(f"[DM ERROR] Could not DM winner {w['uid']}: {e}")
            dm_failed.append(w["uid"])

    for l in losers:
        try:
            user = await bot.fetch_user(int(l["uid"]))
            await user.send(
                f"💸 **You lost.** Market `{market_id}` — **{market['question']}**\n"
                f"You bet **{l['amount']} $KUPID** on **{loser_label}** and lost it all. Better luck next time."
            )
        except Exception as e:
            print(f"[DM ERROR] Could not DM loser {l['uid']}: {e}")
            dm_failed.append(l["uid"])

    if dm_failed:
        failed_mentions = " ".join(f"<@{uid}>" for uid in dm_failed)
        note = f"\n⚠️ Could not DM: {failed_mentions} — they likely have DMs disabled."
    else:
        note = ""

    await interaction.followup.send(
        f"✅ Market `{market_id}` resolved. Winner: **{winner}. {winner_label}**. Bettors have been notified via DM.{note}",
        ephemeral=True
    )

# --- 4. GLOBAL CONFIGURATION & EXPORT ---
@bot.tree.command(name="configure_channel", description="[Admin] Set the channel for resolved market announcements")
@app_commands.describe(channel="The channel to post resolved market results in")
@app_commands.checks.has_permissions(administrator=True)
async def configure_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    data = await load_data()
    data["config"]["announcement_channel_id"] = channel.id
    await save_data(data)
    await interaction.followup.send(f"✅ Announcement channel set to {channel.mention}.", ephemeral=True)

@bot.tree.command(name="configure_bet_log", description="[Admin] Set the channel where all bet actions are logged")
@app_commands.describe(channel="The channel to post bet activity in")
@app_commands.checks.has_permissions(administrator=True)
async def configure_bet_log(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    data = await load_data()
    data["config"]["bet_log_channel_id"] = channel.id
    await save_data(data)
    await interaction.followup.send(f"✅ Bet activity log channel set to {channel.mention}.", ephemeral=True)

@bot.tree.command(name="configure_toggle", description="[Admin] Enable or disable a command")
@app_commands.describe(cmd="The command name to toggle")
@app_commands.checks.has_permissions(administrator=True)
async def configure_toggle(interaction: discord.Interaction, cmd: str):
    await interaction.response.defer(ephemeral=True)
    data = await load_data()
    if cmd in data["config"]["disabled_commands"]:
        data["config"]["disabled_commands"].remove(cmd)
        msg = f"✅ `{cmd}` enabled."
    else:
        data["config"]["disabled_commands"].append(cmd)
        msg = f"🚫 `{cmd}` disabled."
    await save_data(data)
    await interaction.followup.send(msg, ephemeral=True)

@bot.tree.command(name="configure_role", description="[Admin] Restrict a command to a specific role")
@app_commands.describe(cmd="The command name", role="The required role")
@app_commands.checks.has_permissions(administrator=True)
async def configure_role(interaction: discord.Interaction, cmd: str, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    data = await load_data()
    data["config"]["command_roles"][cmd] = role.id
    await save_data(data)
    await interaction.followup.send(f"🔐 `{cmd}` now requires @{role.name}.", ephemeral=True)

@bot.tree.command(name="export_json", description="[Admin] Download a raw JSON backup of all data")
@app_commands.checks.has_permissions(administrator=True)
async def export_json(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = await load_data()
    json_bytes = json.dumps(data, indent=4).encode('utf-8')
    with io.BytesIO(json_bytes) as f:
        await interaction.followup.send("📂 **JSON Backup:**", file=discord.File(f, "market_data.json"), ephemeral=True)

@bot.tree.command(name="export_txt", description="[Admin] Download a TXT ledger (KupidTv username - coins)")
@app_commands.checks.has_permissions(administrator=True)
async def export_txt(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = await load_data()
    users = data.get("users", {})
    kupidtv = data.get("kupidtv_usernames", {})

    report = f"KUPID TERMINAL - ASSET LEDGER\nGenerated: {datetime.datetime.now()}\n{'='*45}\n\n"
    sorted_users = sorted(users.items(), key=lambda item: item[1], reverse=True)
    for i, (uid, bal) in enumerate(sorted_users, 1):
        tv_name = kupidtv.get(uid, f"[Discord:{uid}]")
        report += f"{tv_name} - {bal}\n"

    with io.BytesIO(report.encode('utf-8')) as f:
        await interaction.followup.send("📄 **Text Export:** Here is your readable ledger.", file=discord.File(f, "kupid_ledger.txt"), ephemeral=True)

# --- 5. ADMIN TOOLS ---
@bot.tree.command(name="give", description="[Admin] Add $KUPID to a user's balance")
@app_commands.describe(member="The user to give $KUPID to", amount="Amount to add")
@app_commands.checks.has_permissions(administrator=True)
async def give(interaction: discord.Interaction, member: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    data = await load_data()
    uid = str(member.id)
    data["users"][uid] = data["users"].get(uid, 1000) + amount
    await save_data(data)
    await interaction.followup.send(f"✅ Added **{amount} $KUPID** to {member.mention}.", ephemeral=True)

@bot.tree.command(name="take", description="[Admin] Remove $KUPID from a user's balance")
@app_commands.describe(member="The user to take $KUPID from", amount="Amount to remove")
@app_commands.checks.has_permissions(administrator=True)
async def take(interaction: discord.Interaction, member: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    data = await load_data()
    uid = str(member.id)
    data["users"][uid] = max(0, data["users"].get(uid, 1000) - amount)
    await save_data(data)
    await interaction.followup.send(f"🚨 Deducted **{amount} $KUPID** from {member.mention}.", ephemeral=True)

# --- 6. ERROR HANDLING ---
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        if interaction.response.is_done():
            await interaction.followup.send("⛔ You need Administrator permissions to use this command.", ephemeral=True)
        else:
            await interaction.response.send_message("⛔ You need Administrator permissions to use this command.", ephemeral=True)
    else:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ An error occurred: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ An error occurred: {error}", ephemeral=True)

bot.run(TOKEN)
