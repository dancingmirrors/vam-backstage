"""The scriptable transport and canned Hub live in `vambackstage.devtools`
so the app's hidden `--debug-fake-hub` flag can use them too."""

from vambackstage.devtools.fakehub import FakeHub, FakeTransport, file_route  # noqa: F401
