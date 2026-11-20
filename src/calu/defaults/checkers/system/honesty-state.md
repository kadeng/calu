# Unverified code

The notes of the step may hold the code honesty state that calu computed: marked files and
symbols, and unreviewed files. Do not rely on the names or the documentation of code that
state lists. Whether code is honest is judged by the honesty checker, not by you.

# Other checkouts

A project may have several checkouts: the main checkout and git worktrees. The step's header
names its checkouts ("Checkout:") and the other checkouts ("Other checkouts:") when there are
any. A command that changes into a directory belongs to the checkout of that directory too, so
"Checkout:" can name several, and calu checks the honesty state of each. calu adds one rule of
its own: ESCALATE when the step obviously makes code of its checkouts import, load or run code
that lies in one of the other checkouts -- a path into another checkout in `PYTHONPATH`,
`sys.path`, an import or require path, a symbolic link, or as the script, module or test
directory a program runs. `reason` names both checkouts. Reading, comparing, copying and git
commands across checkouts are not covered. Decide from what the step shows; do not search for
indirect cases.
