---
name: release-addon
description: Test, bump the add-on version, commit and push Training Coach so Home Assistant offers the update
disable-model-invocation: true
---

# Release the Training Coach add-on

Home Assistant only offers an add-on update when `version` in `training_coach/config.yaml` changes,
so every release must bump it. Run these steps in order and stop at the first failure.

1. **Tests:** `.venv/bin/python -m pytest -q`. Stop if anything fails.
2. **UI changes?** If any file under `training_coach/app/templates/` or `training_coach/app/static/` changed since the
   last release (`git diff --name-only $(git describe --tags --abbrev=0 2>/dev/null || git rev-list --max-parents=0 HEAD) HEAD`
   plus uncommitted changes), run the `check-mobile` skill first and fix any overflow.
3. **Bump the version** in `training_coach/config.yaml`: the patch number by default
   (`0.1.1` → `0.1.2`). Use minor if the user says this is a feature release, or if settings or the stored data changed.
   `$ARGUMENTS` may give an explicit version or "minor".
4. **Release notes:** add a `## <version> (<date>)` section at the top of `training_coach/CHANGELOG.md`.
   Write it for the athlete, not the developer: what changed or got fixed, not file names. HA shows this file in the update dialog.
5. **Commit** all intended changes. Check `git status` first and never commit `.shots/`, `data/` or secrets.
   Message: a summary line and what changed for the user, plus the Co-Authored-By trailer.
6. **Push** with the personal GitHub account. The shell's `GITHUB_TOKEN` belongs to the work account and gets a 403:
   ```bash
   git -c credential.helper= -c 'credential.helper=!f(){ echo username=johanlasses; echo "password=$(env -u GITHUB_TOKEN gh auth token -u johanlasses)"; }; f' push
   ```
   Never store this credential in git config or the remote URL.
7. **Tag** the release `v<version>` and push the tag the same way (`... push origin v<version>`).
8. Tell the user the new version and that it appears under Settings → Add-ons → Training Coach → Update in HA.
   A rebuild on the HA Green takes a few minutes.
