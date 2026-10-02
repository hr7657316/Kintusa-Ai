# The fixture repo under agent-repo/ contains its own ``tests/``; it is data for the fixer
# tests and is never collected by the main suite.
collect_ignore_glob = ["*"]
