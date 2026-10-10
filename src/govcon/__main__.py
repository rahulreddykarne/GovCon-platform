"""Allow ``python -m govcon`` on Windows where the console script is optional."""

from govcon.cli import main

if __name__ == "__main__":
    main()
