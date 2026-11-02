"""The installer for people who have never used Linux (issue #37).

`install.sh` (bash) checks the system and starts `scripts/install/install.py`, which asks the
questions in plain language (German or English), writes `.env`, and then runs the project's own
scripts in the right order. The logic lives here, in small modules that a test can drive with a
fake terminal and a fake command runner:

    texts      every sentence the user sees, German and English
    answers    what is asked, how it is checked, and which .env settings the answers become
    envfile    reading, merging and safely writing .env
    system     what the installer looks at on this machine (address, subnet, time zone, system)
    runner     running commands (and the dry run) without ever putting a secret on a command line
    steps      the installation steps, one function each, safe to run again
    cli        the conversation: language, menu for an existing installation, questions, summary
"""

STATE_VERSION = 1
