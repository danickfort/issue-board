"""GitLab merge requests for the stories of a YouTrack or Jira board: g opens the open MRs naming the selected story, G
every open MR of its project's repository. The repository is chosen per tracker project the first time and kept in the
board's per-server state.

GitLab code is imported only once g is used: the boards never need glab. Each board adapts its own issues and projects
through story_project, context_project, prefix and link.
"""
import asyncio
import shutil

from rich.text import Text
from textual import work

from .widgets import CommentDialog, Picker, plain


def demo_gitlab(host):
    """The GitLab board demo's offline GitLab, for the merge requests panel. It never runs glab."""
    from gitlab_board.demo import HOST, DemoClient
    from gitlab_board.glab import GlabError
    if host != HOST:
        raise GlabError(f'The demo has only {HOST}, with demo/portal and demo/api.')
    return DemoClient()


class StoryMergeRequests:
    """Mixed into a BoardView whose __init__ sets gitlab (a GitLab client factory, or None for glab's), gitlabs {} and
    cwd (the checkout whose remotes may name the repository, or None)."""

    def action_merge_requests(self, everything=False):
        """g: open GitLab merge requests naming the selected story; G: every open one of the repository of its project
        (or of the project in scope, or the board's only project). The repository is chosen per project the first time."""
        story = None if everything else self.selected
        project = (self.story_project(self.selected) if self.selected else None) or (everything and self.context_project())
        if not project:
            if everything:
                self.status('Select a story, or choose a project with p: G lists the open merge requests of its GitLab '
                            'repository.', error=True)
            return
        url = self.prefs.data['repos'].get(project.get('id'))
        if url:
            self.open_merge_requests(project, story, url)
        elif self.cwd and (self.gitlab or shutil.which('glab')):
            self.find_repository(project, story)
        else:
            self.ask_repository(project, story)

    @work(exclusive=True, group='repository')
    async def find_repository(self, project, story):
        """A project's first g inside a Git checkout: its GitLab remote, without asking when there is one. Like a typed
        URL, it is saved only once GitLab shows it to you. A repository already linked to another project, or several
        remotes, are asked about instead: one checkout says nothing about which tracker project it belongs to."""
        from gitlab_board import remotes
        from gitlab_board.glab import GlabError
        self.status('Looking for the GitLab project of this checkout…')
        try:
            found, _ = await asyncio.to_thread(remotes.find, self.cwd)
        except remotes.NoRemote:
            self.show_counts()
            self.ask_repository(project, story)
            return
        except GlabError as exc:
            self.status(f'{exc} Nothing was saved; press g to try again.', error=True)
            return
        self.show_counts()
        urls = {f'https://{r.host}/{r.path}': r for r in found}
        if len(urls) == 1:
            url, remote = next(iter(urls.items()))
            taken = [pid for pid, saved in self.prefs.data['repos'].items() if saved.casefold() == url.casefold()]
            if taken:
                others = ', '.join(self.prefix(pid) or pid for pid in taken)
                self.notify(f'This checkout’s repository is already linked to {others}. Confirm it for '
                            f'{project.get("shortName")} too, or enter another.', markup=False)
                self.ask_repository(project, story, url)
            else:
                self.open_merge_requests(project, story, url, check=True, remote=remote.names)
            return
        ask = '\x00ask'
        choices = [(url, Text.assemble(f'{r.host}/{r.path}', (f'  {r.names}', 'dim')), f'{url} {r.names}'.casefold(), False)
                   for url, r in urls.items()]
        choices.append((ask, Text('Another repository URL…', 'italic'), None, False))
        self.push_screen(Picker(f'GitLab repository of {project.get("shortName")} stories · this checkout has several',
                                choices), lambda url: self.ask_repository(project, story) if url == ask else url and
                         self.open_merge_requests(project, story, url, check=True, remote=urls[url].names))

    def ask_repository(self, project, story, current=''):
        if self.gitlab is None and not shutil.which('glab'):
            self.status('Merge requests come from GitLab through its CLI. Install it (brew install glab), sign in with '
                        'glab auth login --hostname <your GitLab host>, then press g again.', error=True)
            return
        from gitlab_board.merge_requests import repository
        from gitlab_board.target import TargetError

        def chosen(text):
            if text:
                try:
                    host, path = repository(text)
                except TargetError as exc:
                    self.status(f'Not a GitLab project URL: {exc} Nothing was saved; press g to try again.', error=True)
                    return
                self.open_merge_requests(project, story, f'https://{host}/{path}', check=True)
        self.push_screen(CommentDialog(f'GitLab repository of {project.get("shortName")} stories',
                                       'https://gitlab.example.com/group/project', current), chosen)

    def open_merge_requests(self, project, story, url, check=False, remote=None):
        """The panel for a repository: the MRs naming the story, or all open ones without one. A new repository (check)
        is saved for the project once GitLab shows it to you. remote: the checkout's Git remote it was found in."""
        from gitlab_board.merge_requests import MergeRequestsScreen, repository
        from gitlab_board.target import TargetError
        try:
            host, path = repository(url)
        except TargetError:  # a hand-edited state file: ask again
            self.ask_repository(project, story, url)
            return
        key = story and self.ref(story)

        async def load():
            gitlab = self.gitlab_client(host)
            if check:
                screen.home = (await gitlab.place(path, 'project'))['path']
                self.prefs.data['repos'][project['id']] = f'https://{host}/{screen.home}'
                self.save_prefs()
                found = f', the {remote} remote of this checkout' if remote else ''
                self.notify(f'{project.get("shortName")} merge requests now come from {host}/{screen.home}{found}. '
                            'Press u in the panel to change it.', markup=False)
            if key:
                return await gitlab.project_merge_requests(screen.home, key)
            found = await gitlab.open_merge_requests({'kind': 'project', 'path': screen.home})
            for mr in found['mrs']:
                named = self.named_stories(mr, host, screen.home)
                mr['why'] = 'Names ' + '; '.join(f'{k} in its {where}' for k, where in named.items()) if named else \
                    'Names no story of a project linked to this repository'
            return found

        async def links(mr):
            return [{**self.link(k), 'ref': k, 'why': f'named in its {where}',
                     'title': next((i['summary'] for i in self.issues_data if self.ref(i) == k), '')}
                    for k, where in self.named_stories(mr, host, screen.home).items()]
        if key:
            screen = MergeRequestsScreen(f'Open merge requests · {key} {plain(story["summary"])}',
                                         f'In {host}/{path}, naming {key} in title, branch or description',
                                         load, host, path, repo=True, links=links)
        else:
            screen = MergeRequestsScreen(f'All open merge requests · {host}/{path}',
                                         f'Most recently updated first · i opens a {project.get("shortName")} story an MR '
                                         'names', load, host, path, repo=True, links=links,
                                         empty='No open merge request in this repository.')
        self.push_screen(screen, lambda choice: self.ask_repository(project, story, url) if choice == 'repo' else
                         self.merge_requests_closed(choice))

    def named_stories(self, mr, host, path):
        """{story ID: where} for stories an MR names, of the tracker projects whose repository this is: the link g
        follows. Prefixes of other projects are never matched."""
        from gitlab_board.client import named
        repo = f'https://{host}/{path}'.casefold()
        prefixes = [self.prefix(pid) for pid, saved in self.prefs.data['repos'].items() if saved.casefold() == repo]
        return named([p for p in prefixes if p], mr)

    def gitlab_client(self, host):
        if host not in self.gitlabs:
            from gitlab_board.client import Client as GitLab  # only once g is used: the board never needs glab
            self.gitlabs[host] = (self.gitlab or GitLab)(host)
        return self.gitlabs[host]
