-- Before calu shipped empty rule lists, `calu setup` filled the six global lists with default
-- rules. Rules are overrides of the checkers: an allow rule lets a step skip them, a deny rule
-- stops a step without asking them, and an allow rule matches a command by its text, whatever
-- program that text starts. They are meant to be the exception, written by the owner
-- (PLAN.md D179, D198). This removes the rules of the global scope that are exactly the ones
-- old versions seeded; rules with another pattern, and every project's rules, stay.

DELETE FROM rules r
 USING (VALUES
    ('command-allow', $calu$^(ls|pwd|cat|head|tail|wc|file|stat|du|df|which|type|whoami|date|uname|basename|dirname|realpath|readlink|true|false)( |$)$calu$),
    ('command-allow', $calu$^(echo|printf)( |$)$calu$),
    ('command-allow', $calu$^(grep|egrep|fgrep|diff|cmp|cut|tr|column|nl)( |$)$calu$),
    ('command-allow', $calu$^find(?!.* -(exec|execdir|ok|okdir|delete|fprint|fprint0|fprintf|fls)( |$))( |$)$calu$),
    ('command-allow', $calu$^cd( |$)$calu$),
    ('command-allow', $calu$^git (status|log|diff|show|blame|rev-parse|ls-files|describe|shortlog)(?!.* --(output|ext-diff|textconv))( |$)$calu$),
    ('command-allow', $calu$^git branch( (--list|-a|-r|-v|-vv|--show-current))*$$calu$),
    ('command-allow', $calu$^git remote( -v)?$$calu$),
    ('command-deny', $calu$^(sudo|su|doas)( |$)$calu$),
    ('command-deny', $calu$^git( -[Cc] \S+)* push(?= )(?=.* (--force(-with-lease)?(=\S*)?|-[a-zA-Z]*[fd][a-zA-Z]*|--mirror|--delete|--prune|[+:]\S+)( |$))$calu$),
    ('command-deny', $calu$^git reset --hard( |$)$calu$),
    ('command-deny', $calu$^git clean( |$)$calu$),
    ('command-deny', $calu$^rm(?=.* -[a-zA-Z]*[rR])(?=.* (/|~|\$HOME)/?( |$))$calu$),
    ('command-deny', $calu$^(sh|bash|zsh|dash)$$calu$),
    ('command-deny', $calu$^(sh|bash|zsh|dash) -[a-zA-Z]*c( |$)$calu$),
    ('command-deny', $calu$^eval( |$)$calu$),
    ('command-deny', $calu$^(mkfs|mkfs\.\S+|fdisk|parted|shutdown|reboot|poweroff)( |$)$calu$),
    ('read-allow', $calu$^[^/]$calu$),
    ('read-allow', $calu$^/tmp(/|$)$calu$),
    ('read-allow', $calu$^/dev/null$$calu$),
    ('read-deny', $calu$(^|/)\.env(\.(?!(template|example|sample)$)[^/]*)?$$calu$),
    ('read-deny', $calu$(^|/)\.ssh(/|$)$calu$),
    ('read-deny', $calu$\.(pem|key|p12|pfx|secret|token)$$calu$),
    ('read-deny', $calu$(^|/)\.(netrc|pgpass|npmrc|pypirc|git-credentials)$$calu$),
    ('read-deny', $calu$(^|/)\.aws/credentials$$calu$),
    ('read-deny', $calu$(^|/)\.docker/config\.json$$calu$),
    ('read-deny', $calu$(^|/)\.kube/config$$calu$),
    ('read-deny', $calu$(^|/)\.config/gh/hosts\.yml$$calu$),
    ('read-deny', $calu$(^|/)\.claude/\.credentials\.json$$calu$),
    ('read-deny', $calu$(^|/)\.(bashrc|zshrc)$$calu$),
    ('read-deny', $calu$^/(etc|proc|sys|boot|bin)(/|$)$calu$),
    ('write-allow', $calu$^[^/]$calu$),
    ('write-allow', $calu$^/tmp(/|$)$calu$),
    ('write-allow', $calu$^/dev/(null|stdout|stderr)$$calu$),
    ('write-deny', $calu$(^|/)\.claude(/|$)$calu$),
    ('write-deny', $calu$(^|/)\.claude\.json$$calu$),
    ('write-deny', $calu$^CLAUDE\.md$$calu$),
    ('write-deny', $calu$^AGENTS\.md$$calu$),
    ('write-deny', $calu$^\.mcp\.json$$calu$),
    ('write-deny', $calu$^\.checker_training_data(/|$)$calu$),
    ('write-deny', $calu$^\.git(/|$)$calu$),
    ('write-deny', $calu$^\.vscode/(tasks|launch)\.json$$calu$),
    ('write-deny', $calu$(^|/)\.env(\.[^/]*)?$$calu$),
    ('write-deny', $calu$(^|/)\.ssh(/|$)$calu$),
    ('write-deny', $calu$(^|/)\.(bashrc|zshrc|profile|bash_profile|zprofile)$$calu$)
 ) AS seeded(list, pattern)
 WHERE r.project_id IS NULL
   AND r.list = seeded.list
   AND r.pattern = seeded.pattern;
