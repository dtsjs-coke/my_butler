import os
import requests
from sympy import sympify, solve, symbols, diff, integrate
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables
# .env 파일이 상위 폴더(my_butler 루트)에 있으므로 상대 경로로 로드
ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(ENV_PATH)

class MathEvaluator:
    @staticmethod
    def safe_evaluate(expression):
        """Evaluates mathematical expressions safely using SymPy."""
        try:
            # sympify parses the string into a SymPy expression
            # evalf() evaluates it to a numerical value
            expr = sympify(expression)
            result = expr.evalf() if hasattr(expr, 'evalf') else expr
            return result
        except Exception as e:
            return f"Error: {str(e)}"

class MathCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api_url = "http://localhost:5000/send"

    @commands.command(name="calc")
    async def calculate(self, ctx, *, expression: str):
        """Calculates math expressions and sends result via Local API."""
        # Sanitize and Evaluate
        result = MathEvaluator.safe_evaluate(expression)
        
        # Prepare result message
        output_text = f"🔢 **Math Calculation**\nInput: `{expression}`\nResult: **{result}**"
        
        # Send via Local API as per requirement
        payload = {
            "channel_id": int(os.getenv("CHAT_CHANNEL_ID")),
            "content": output_text
        }
        
        try:
            response = requests.post(self.api_url, json=payload)
            if response.status_code != 200:
                await ctx.send("⚠️ Failed to relay response through Local API.")
        except Exception as e:
            await ctx.send(f"⚠️ API Connection Error: {str(e)}")

def setup(bot):
    """Standard Cog setup function."""
    bot.add_cog(MathCog(bot))