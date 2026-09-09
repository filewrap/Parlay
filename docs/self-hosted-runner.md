# Self-hosted CI runner (on your VPS)

Parlay's CI runs on a self-hosted GitHub Actions runner on your own VPS, so
lint, type checks, and tests run in the same environment the bot runs in. CI
does not make live Telegram or Gemini calls; the live audio path stays a manual
test.

The workflow targets a runner with the labels `self-hosted` and `parlay`
(see `.github/workflows/ci.yml`).

## Register the runner

1. On GitHub, open the repository: **Settings > Actions > Runners > New self-hosted runner**.
2. Pick Linux / x64 and follow the shown download commands. Then configure with
   the token GitHub gives you and add the `parlay` label:

   ```bash
   ./config.sh --url https://github.com/filewrap/Parlay --token <RUNNER_TOKEN> --labels parlay
   ```

3. Install prerequisites on the VPS so jobs can run:

   ```bash
   sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv ffmpeg git
   ```

4. Run the runner as a service so it survives reboots:

   ```bash
   sudo ./svc.sh install
   sudo ./svc.sh start
   ```

## Verify

Push a commit or open a pull request against `main`. The **CI** workflow should
pick up on your runner and run lint, format check, type check, and tests.

## Notes

- Keep the runner on a machine you trust; self-hosted runners execute workflow
  code. This repo is single-operator, which fits that model.
- If the runner is offline, CI stays queued until it comes back.
