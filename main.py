import discord
from discord import app_commands, Interaction, ButtonStyle
from discord.ext import commands
import sqlite3
from datetime import datetime, timedelta, timezone
import logging
from dotenv import load_dotenv
import os


# Initialize logging
logging.basicConfig(level=logging.INFO)

# Initialize database
conn = sqlite3.connect('bets.db')
c = conn.cursor()

# Create tables if they don't exist
c.execute('''
    CREATE TABLE IF NOT EXISTS bets (
        message_id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        option1 TEXT NOT NULL,
        option2 TEXT NOT NULL,
        creator_id INTEGER NOT NULL
    )
''')

c.execute('''
    CREATE TABLE IF NOT EXISTS individual_bets (
        message_id INTEGER,
        user_id INTEGER,
        chosen_option TEXT,
        amount INTEGER,
        FOREIGN KEY(message_id) REFERENCES bets(message_id)
    )
''')

c.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER NOT NULL
    )
''')

conn.commit()

# Utility functions


def get_user_balance(user_id: int) -> int:
    c.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    if result:
        return result[0]
    else:
        # Initialize user with default balance
        c.execute(
            'INSERT INTO users (user_id, balance) VALUES (?, ?)', (user_id, 1000))
        conn.commit()
        return 1000


def set_user_balance(user_id: int, amount: int):
    c.execute('UPDATE users SET balance = ? WHERE user_id = ?',
              (amount, user_id))
    conn.commit()


last_daily_claim = {}  # {user_id: datetime}
DAILY_AMOUNT = 1000
DAILY_COOLDOWN = timedelta(hours=24)


def can_claim_daily(user_id: int) -> bool:
    last_claim = last_daily_claim.get(user_id, None)
    if last_claim is None:
        return True
    return datetime.now(timezone.utc) - last_claim >= DAILY_COOLDOWN


def claim_daily(user_id: int):
    last_daily_claim[user_id] = datetime.now(timezone.utc)
    current_balance = get_user_balance(user_id)
    set_user_balance(user_id, current_balance + DAILY_AMOUNT)

# Define the BettingView with buttons


class BettingView(discord.ui.View):
    def __init__(self, creator_id: int, title: str, option1: str, option2: str):
        super().__init__(timeout=None)
        self.creator_id = creator_id
        self.title = title
        self.option1 = option1
        self.option2 = option2
        self.message_id = None  # Will be set after sending the message

    @discord.ui.button(label="Option 1", style=ButtonStyle.primary)
    async def option1_button(self, interaction: Interaction, button: discord.ui.Button):
        await self.place_bet(interaction, self.option1)

    @discord.ui.button(label="Option 2", style=ButtonStyle.primary)
    async def option2_button(self, interaction: Interaction, button: discord.ui.Button):
        await self.place_bet(interaction, self.option2)

    @discord.ui.button(label="End Bet", style=ButtonStyle.danger)
    async def end_bet_button(self, interaction: Interaction, button: discord.ui.Button):
        # Only the creator or an admin can end the bet
        if interaction.user.id != self.creator_id and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You are not allowed to end this bet!", ephemeral=True)
            return

        # Fetch bet details from the database
        c.execute(
            'SELECT title, option1, option2 FROM bets WHERE message_id = ?', (self.message_id,))
        bet = c.fetchone()
        if not bet:
            await interaction.response.send_message("Bet data not found.", ephemeral=True)
            return

        title, option1, option2 = bet

        # Tally up results
        c.execute('SELECT chosen_option, SUM(amount) FROM individual_bets WHERE message_id = ? GROUP BY chosen_option', (self.message_id,))
        results = c.fetchall()

        option_totals = {option1: 0, option2: 0}
        for chosen_option, total in results:
            option_totals[chosen_option] = total

        if option_totals[option1] > option_totals[option2]:
            winning_option = option1
        elif option_totals[option2] > option_totals[option1]:
            winning_option = option2
        else:
            winning_option = None

        # Find winners
        if winning_option:
            c.execute('SELECT user_id, amount FROM individual_bets WHERE message_id = ? AND chosen_option = ?',
                      (self.message_id, winning_option))
            winners = c.fetchall()
            reward_multiplier = 2  # Example: double the bet

            for user_id, amount in winners:
                current_balance = get_user_balance(user_id)
                new_balance = current_balance + \
                    (amount * (reward_multiplier - 1))
                set_user_balance(user_id, new_balance)

            # Announce results
            mentions = []
            for user_id, _ in winners:
                member = interaction.guild.get_member(user_id)
                if member:
                    mentions.append(member.mention)
                else:
                    mentions.append(f"User ID: {user_id}")
            winner_mentions = ", ".join(mentions)
            await interaction.response.send_message(
                f"The bet *{title}* has ended!\nWinning option: **{winning_option}**\nWinners: {winner_mentions}\nThey each received {reward_multiplier}x their bet!"
            )
        else:
            await interaction.response.send_message(f"The bet *{title}* ended in a tie. No winners!")

        # Disable all buttons after ending the bet
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

        # Remove bet from the database
        c.execute('DELETE FROM bets WHERE message_id = ?', (self.message_id,))
        c.execute('DELETE FROM individual_bets WHERE message_id = ?',
                  (self.message_id,))
        conn.commit()

    async def place_bet(self, interaction: Interaction, chosen_option: str):
        # Prompt user for bet amount via Modal
        await interaction.response.send_modal(BetAmountModal(self.message_id, chosen_option))

# Define the BetAmountModal


class BetAmountModal(discord.ui.Modal, title="Enter Bet Amount"):
    bet_amount = discord.ui.TextInput(
        label="Amount to Bet",
        style=discord.TextStyle.short,
        placeholder="Enter a number",
        required=True
    )

    def __init__(self, message_id: int, chosen_option: str):
        super().__init__()
        self.message_id = message_id
        self.chosen_option = chosen_option

    async def on_submit(self, interaction: Interaction):
        amount_str = self.bet_amount.value.strip()
        if not amount_str.isdigit():
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)
            return
        amount = int(amount_str)
        if amount <= 0:
            await interaction.response.send_message("You must bet a positive amount.", ephemeral=True)
            return

        user_id = interaction.user.id
        balance = get_user_balance(user_id)
        if balance < amount:
            await interaction.response.send_message("You don't have enough coins to place that bet.", ephemeral=True)
            return

        # Deduct bet amount
        set_user_balance(user_id, balance - amount)

        # Store the bet in the database
        c.execute('INSERT INTO individual_bets (message_id, user_id, chosen_option, amount) VALUES (?, ?, ?, ?)',
                  (self.message_id, user_id, self.chosen_option, amount))
        conn.commit()

        await interaction.response.send_message(
            f"You've placed a bet of {amount} coins on *{self.chosen_option}*! Your new balance: {get_user_balance(user_id)} coins.",
            ephemeral=True
        )


# Initialize the bot intents
intents = discord.Intents.default()
intents.message_content = False  # Set to True if you enabled Message Content Intent
intents.members = True  # Enable if you enabled Server Members Intent
# Add other intents if necessary

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f'Logged in as {bot.user}!')


@bot.tree.command(name="create_bet", description="Create a new bet with two options.")
@app_commands.describe(
    title="Title of the bet",
    option1="Option 1 name",
    option2="Option 2 name"
)
async def create_bet(interaction: Interaction, title: str, option1: str, option2: str):
    view = BettingView(creator_id=interaction.user.id,
                       title=title, option1=option1, option2=option2)
    embed = discord.Embed(
        title=title, description=f"*Option 1:* {option1}\n**Option 2:** {option2}")

    # Send the message without ephemeral=True to make it public
    await interaction.response.send_message(embed=embed, view=view)

    # Retrieve the sent message object correctly
    msg_obj = await interaction.original_response()

    # Store bet data using the correct message ID
    view.message_id = msg_obj.id  # Assign the message ID to the view
    c.execute('INSERT INTO bets (message_id, title, option1, option2, creator_id) VALUES (?, ?, ?, ?, ?)',
              (msg_obj.id, title, option1, option2, interaction.user.id))
    conn.commit()

    logging.info(f"Created bet with message ID: {msg_obj.id}")


@bot.tree.command(name="money", description="Get your daily coins.")
async def money(interaction: Interaction):
    user_id = interaction.user.id
    if can_claim_daily(user_id):
        claim_daily(user_id)
        balance = get_user_balance(user_id)
        await interaction.response.send_message(
            f"You have claimed your daily {DAILY_AMOUNT} coins! Your new balance: {balance} coins."
        )
    else:
        await interaction.response.send_message("You have already claimed your daily coins. Try again later!", ephemeral=True)


@bot.tree.command(name="profile", description="Check a user's profile.")
@app_commands.describe(user="The user whose profile you want to see")
async def profile(interaction: Interaction, user: discord.User):
    balance = get_user_balance(user.id)
    await interaction.response.send_message(f"{user.name}'s balance: {balance} coins.")


# Run the bot with your actual token
API_KEY = os.getenv("DISCORD_API_KEY")
bot.run(API_KEY)

last_daily_claim
