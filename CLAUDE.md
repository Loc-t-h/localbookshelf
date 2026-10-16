# LocalBookshelf

See [README.md](README.md) for usage and [localbookshelf_specs.md](localbookshelf_specs.md)
for the full spec and the reasoning behind the scope.

## This repository is public

Nothing identifiable about the author or their library may be committed. This
covers code, comments, tests, fixtures, documentation, commit messages and pull
request descriptions equally — a commit message is as public as a source file,
and harder to take back.

Never commit:

- **Real filesystem paths** from a personal machine. Use `/srv/books`,
  `~/Books`, or `/path/to/books`.
- **Names, emails, usernames, hostnames, IP or MAC addresses**, or anything
  read out of `git config user.*`.
- **Anything from the author's actual library** — titles, authors, ISBNs,
  series names, folder names, or cover images. This is the easy one to get
  wrong: a bug found in a real file is tempting to document with that file.
- **Screenshots, HTML dumps, or database exports** from a running shelf.

`.env` holds the real library path and is gitignored. It stays that way;
`.env.example` carries placeholders only.

### Writing about a bug found in a real file

Describe the *shape* of the problem, not the file it turned up in: "an ISBN
written into `dc:title`", not the book that happened to have it. Then invent a
title for the test and the comment. `"[Harrow Cycle 02] - Second Light"`
exercises the same code path as a real series prefix and tells a reader nothing
about anyone's shelf.

Invented ISBNs should be obviously fake (`9781234567897`, `0123456789`) rather
than a real book's, so nobody has to wonder whether it came from the library.
