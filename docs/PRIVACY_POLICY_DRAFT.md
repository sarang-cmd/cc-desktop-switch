# Privacy Policy Draft for Review

**Status: draft. This document is not a finalized privacy policy and has not been reviewed by legal counsel.** Before publication, the project maintainer must add the responsible entity and contact address, confirm applicable jurisdictions, review the listed data flows, and approve retention and support procedures.

## Product and scope

This draft describes the local Junction desktop application. It is not a policy for Anthropic, Claude Desktop, GitHub, or any API provider that you configure.

## Data stored on your computer

The application stores provider settings, model routes, API keys, language and theme preferences, and other app settings in its local configuration directory. The default directory is `~/.cc-desktop-switch`; `CCDS_CONFIG_DIR` can select a different location. Configuration backups may also be written there. Local diagnostic logs are maintained for troubleshooting.

The app does not provide a user account or cloud configuration synchronization feature.

## Network requests

- When you use a provider, the application forwards your request to the provider endpoint you configured. The request can include prompts, model choices, and the authentication material required by that provider. The configured provider's terms and privacy practices apply to its processing of that request.
- The optional upstream proxy setting routes those requests through the proxy address you configure.
- The app can check the configured update URL. The default update manifest is hosted in this project's GitHub Releases. If you choose to install an update, the app downloads the release package and checks its SHA-256 value before handing it to the platform installer.
- The local admin API is protected by a runtime token and is intended for the local app UI.

The project repository does not document a remote account, cloud-sync service, or analytics backend. Maintainers should re-check this statement whenever network behavior changes.

## Public website

The product website is a static GitHub Pages site. Its homepage requests the latest release details from the GitHub API to display the current version and create direct download links. Downloads are served by GitHub. GitHub's privacy terms govern its hosting, API, download services, and service logs; the project does not control GitHub's server-side retention.

## Diagnostics and support

Diagnostics are created on request. The export can contain app and operating-system versions, provider names and identifiers, API format, authentication scheme, whether an API key is configured, sanitized endpoint details, mapped model slots, local proxy state, and recent redacted logs. It is not sent automatically by the app. Review an export before sharing it because log text and endpoint details can still reveal information about your setup.

Never share an API key, access token, private endpoint, or unreviewed diagnostic export in a public issue.

## Retention, deletion, and rights requests

Configuration, backups, and logs remain on the user's computer and are subject to the user's local backup and deletion practices. This project does not currently provide a central account or a server-side profile to delete.

**Maintainer review required:** specify an official support contact, retention practices for any voluntarily submitted issue or diagnostic attachment, applicable privacy rights and request process, and the project's legal entity and jurisdiction before treating this draft as a public policy.

## Changes and contact

**Maintainer review required:** add the official privacy contact and a process for announcing material changes before publication.
