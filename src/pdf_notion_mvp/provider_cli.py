"""Configuration preview only: never loads keys, .env files or provider clients."""
import argparse

from .providers import KEY_NAMES, ProviderSettings
from .quiz import GenerationBlocked


def main():
    parser = argparse.ArgumentParser(description="Check non-secret provider settings; no model call")
    parser.add_argument("--provider", choices=["gemini", "openai"])
    parser.add_argument("--model")
    args = parser.parse_args()
    try:
        settings = ProviderSettings.from_env()
        if args.provider: settings.provider = args.provider
        if args.model is not None: settings.model = args.model
    except GenerationBlocked:
        parser.error("unsupported provider configuration")
    print(f"provider={settings.provider} key_name={KEY_NAMES[settings.provider]} "
          f"model_configured={bool(settings.model)} model_approved={settings.model in settings.approved_models} "
          "network=disabled key_lookup=disabled dotenv_load=disabled")


if __name__ == "__main__":
    main()
