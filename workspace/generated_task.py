import os
import re
import requests
import discord
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables
load_dotenv("/data/data/com.termux/files/home/dev_pjt/my_butler/.env")

TOKEN = os.getenv("DISCORD_TOKEN")
CHAT_CHANNEL_ID = int(os.getenv("CHAT_CHANNEL_ID"))

# Discord Bot Setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

def safe_evaluate(expression):
    # Only allow digits, basic operators, and parentheses for security
    if not re.match(r'^[0-9+\-*/().\s]+$', expression):
        return "Error: Illegal characters. Only numbers and +-*/() are allowed."
    
    try:
        # Evaluate in a restricted environment (no built-ins)
        # Note: eval is used here after strict regex filtering to minimize risk
        result = eval(expression, {"__builtins__": None}, {})
        return result
    except ZeroDivisionError:
        return "Error: Division by zero is not allowed."
    except SyntaxError:
        return "Error: Invalid mathematical syntax."
    except Exception as e:
        return f"Error: {str(e)}"

@bot.command(name="calc")
async def calc(ctx, *, expression: str):
    """Calculates mathematical expressions securely."""
    # 1. Evaluate logic
    result = safe_evaluate(expression)
    
    # 2. Format result
    response_text = f"\ud83d\udd22 **계산기 결과**\n**수식:** `{expression}`\n**결과:** `{result}`"

    # 3. Use Local API to send the message as per system design requirements
    api_url = "http://localhost:5000/send"
    payload = {
        "channel_id": CHAT_CHANNEL_ID,
        "content": response_text
    }
    
    try:
        requests.post(api_url, json=payload)
    except Exception as e:
        print(f"Local API call failed: {e}")
        await ctx.send(response_text) # Fallback to direct send if API fails

if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("DISCORD_TOKEN: ******** found in environment.")