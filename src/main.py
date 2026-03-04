from .signal_proc import cortex.py
from .signal_proc.cortex.py import Cortex

import os
from dotenv import load_dotenv

# Load the variables from the .env file
load_dotenv() 

# Now you can access it like a normal environment variable
CLIENT_ID = os.getenv("CLIENT_ID")

if not CLIENT_ID:
    raise ValueError("CLIENT_ID not found! Did you run setup_env.py?")

print(f"CLIENT_ID loaded successfully")

CLIENT_SECRET = os.getenv("CLIENT_SECRET")

if not CLIENT_SECRET:
    raise ValueError("CLIENT_SECRET not found! Did you run setup_env.py?")

print(f"CLIENT_SECRET loaded successfully")
