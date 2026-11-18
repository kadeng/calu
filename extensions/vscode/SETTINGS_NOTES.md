# Settings dialog: notes on the contract

For the authors of the host side and of tests. The settings webview (`webview/settings.ts`,
`settingsCards.ts`, `settingsRows.ts`, `settingsDom.ts`; pure logic in
`src/shared/settingsView.ts`) uses the messages of `src/shared/settingsMessages.ts` unchanged.
No message, type or host file was changed.

## What the dialog sends

| action in the dialog | message |
|---|---|
| start, scope switch, "Try again", 300 ms after a saved model setting | `load {project}` |
| any setting row; Claude Code's model, effort and Bedrock switch in the Models card; the honesty mode | `set {key, value, project}` |
| "Reset to default" / "Remove project value" | `unset {key, project}` |
| "Apply" of the confirm bar | `setAllModels {model, effort?, project}`; `model` is the preset's model string, "default" for the preset without a model, or the custom text |
| a picker of "Per role" | `setRoleModel {role, model, project}` ("" = default) |
| "Edit instructions" | `editChecker {name}` |
| "Check again" | `runStatus` |
| fix buttons | `fix {action}`: `openClaudeDocs`, `updateCalu`, `installCalu`, `restartBridge`, `startServer`, `copyServerCommand`, `copyRootInstall` |

After `saved` for `claude.model`, `claude.effort`, `claude.use_bedrock` or a key that is a model
role, the dialog sends `load` again, because `ide.models.presets` (default model, roles) may
have changed.

## What the contract does not offer

- **Enable or disable a checker**: no message exists, so the Checkers card shows "Enabled" /
  "Disabled" read-only.
- **Show checker context**: only the chat has `showContext`; the settings dialog has no button.
- `ide.models.presets` without a database login returns `claude: null`, `roles: []` and
  `problem` (spec, "Deviations"); `IdeModelsResult` does not type this. The dialog reads these
  members defensively: the preset cards are shown disabled with the `problem` text, and
  "Claude Code itself" is left out.

## `data-testid`

`WEBVIEW_CONTRACT.md` is followed where it names an id.

| element | id |
|---|---|
| search field, its clear button, "Modified" chip, result count | `settings-search`, `settings-search-clear`, `settings-only-modified`, `settings-count` |
| empty state, its button | `settings-empty`, `settings-empty-clear` |
| scope select, save status, progress line, problems, retry | `scope-select`, `save-status`, `settings-busy`, `settings-problems`, `settings-retry` |
| navigation: a settings section, a card, the select of narrow widths | `settings-nav-<sectionId>`, `settings-nav-card-<setup/models/checkers/honesty>`, `settings-nav-select` |
| a settings section | `settings-section-<sectionId>` |
| a setting: control, row, reset, error line | `setting-<key>`, `setting-row-<key>`, `setting-reset-<key>`, `setting-error-<key>`; the custom field of a model picker: `setting-<key>-custom` |
| cards | `setup-card`, `models-card`, `checkers-card`, `honesty-card`; error lines `<card>-error` |
| Setup | `setup-toggle`, `setup-summary`, `setup-item-<name>`, `setup-fix-<name>` (a second fix: `setup-fix-<name>-<action>`), `server-help`, `server-command`, `server-copy`, `server-start`, `rootInstall-toggle`, `root-install-command`, `setup-fix-root-install`, `setup-check`, `setup-update` |
| Models | `preset-<id>`, `preset-custom`, `models-effort`, `models-confirm`, `models-apply`, `models-cancel`, `roles-toggle`, `roles-summary`, `role-<key>` (custom field: `role-<key>-custom`), `claude-model` (`claude-model-custom`), `claude-effort`, `claude-bedrock` |
| Checkers | `checker-<name>`, `checker-edit-<name>` |
| Honesty | `honesty-mode-<off/track/enforce>`, `honesty-link-<key>` |
