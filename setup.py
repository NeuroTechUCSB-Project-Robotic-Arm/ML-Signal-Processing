import os
from pathlib import Path

def setup_project_secrets():
    env_file = Path(".env")
    
    # 1. Prompt for the API Key
    print("--- 🔐 Project Secret Setup ---")
    client_id = input("Enter your Client ID: ").strip()
    
    if not client_id:
        print("❌ Error: Client ID cannot be empty.")
        return

    client_secret = input("Enter your Client Secret: ").strip()

    if not client_secret:
        print("❌ Error: Client Secret cannot be empty.")
        return

    # 2. Create/Update the .env file
    with open(env_file, "w") as f:
        f.write(f"CLIENT_ID={client_id}\n")
        f.write(f"CLIENT_SECRET={client_secret}\n")
    
    print(f"✅ Created {env_file}")

    print("\nSetup complete! You can now use your keys in the project.")

if __name__ == "__main__":
    setup_project_secrets()
