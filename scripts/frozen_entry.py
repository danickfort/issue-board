"""Shared executable entry point; preserve the four installed command names."""
from pathlib import Path
import sys

from issueboard.app import main as issueboard
from youtrack_board.app import main as youtrack
from gitlab_board.app import main as gitlab
from jira_board.app import main as jira

if __name__ == '__main__':
    commands = {'issueboard': issueboard, 'youtrack-board': youtrack,
                'gitlab-board': gitlab, 'jira-board': jira}
    commands[Path(sys.argv[0]).name]()
