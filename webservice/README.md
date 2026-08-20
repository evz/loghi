# Loghi Web Service

This guide provides instructions for deploying and using the Loghi framework in a dockerized environment, utilizing APIs for a seamless workflow in handwritten text recognition and layout analysis.

## Environment Setup

The deployment uses Docker and Docker Compose, simplifying the setup and eliminating concerns about local environment variations. This README is located in the `webservice` directory, containing all you need to get started.

### Directory Overview

- `loghi-tooling/`: Contains `configuration.yml` for tooling configuration.
- `webservice-scripts/`: Includes example scripts for each part of the pipeline, designed to demonstrate how to integrate and automate various Loghi components.
- `docker-compose.yml`: An example Docker Compose file to orchestrate the startup of all web services (tooling, HTR, and Laypa) with a simple `docker compose up` command.
- `orchestrator/`: A small native Python service that wraps the whole multi-step pipeline behind a single HTTP endpoint. See [Orchestrator](#orchestrator-single-call-transcription) below.

## Getting Started

### Starting the Services

To initialize the Loghi web services:

1. Ensure Docker and Docker Compose are installed on your system.

2. Start the Docker containers with the following command:

    ```bash
    docker compose up
    ```

   This boots up the necessary Docker containers and provides a log of the operations. Ensure you have Docker Compose version `1.28.0` or higher for proper GPU support, if required. 

### Processing Workflow

The Loghi framework provides a flexible pipeline for processing handwritten texts. Here is a generalized workflow to guide your usage:

1. **Baseline Detection:** Use Laypa to identify text baselines and regions in your documents, preparing them for HTR.

2. **Image Preprocessing:** If needed, preprocess images to enhance text recognition accuracy, such as line extraction and image normalization.

3. **Handwritten Text Recognition (HTR):** Process the prepared images through Loghi HTR to transcribe the text.

4. **Post-processing:** Apply necessary post-processing steps, such as merging HTR results into PageXML format, recalculating reading order, and splitting text into words.

5. **Integration and Automation:** Utilize the `webservice-scripts/` as templates to automate the workflow and integrate Loghi components into your system. For more information on the available scripts, refer to the `webservice-scripts/README.md` file.

## Orchestrator: Single-Call Transcription

Driving the pipeline by hand means 5+ chained HTTP calls (`do-laypa.sh` -> `extract-baselines.sh` -> `cut-from-image.sh` -> `do-htr.sh` -> `htr-merge-page-xml.sh`, each with its own async status polling). The `orchestrator/` directory wraps all of that behind one endpoint: send it an image, get back finished PageXML (or plain text).

It's a plain Python/FastAPI process with no GPU/ML dependencies of its own — it only makes HTTP calls to laypa/htr/loghi-tooling and reads/writes the same shared directories they use. It runs natively on the host rather than in Docker, managed by a systemd unit so it survives reboots and crashes. Since it's GPU-free, enabling it at boot doesn't touch GPU memory — the `laypa`/`htr`/`loghi-tooling` containers are governed separately (see below).

### One-time setup

```bash
cd webservice/orchestrator
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Install the systemd unit (adjust `User`/`Group`/paths if your checkout lives elsewhere):

```ini
# /etc/systemd/system/loghi-orchestrator.service
[Unit]
Description=Loghi orchestrator (single-call HTR transcription endpoint)
After=network.target

[Service]
Type=simple
User=eric
Group=eric
WorkingDirectory=/home/eric/code/loghi/webservice/orchestrator
Environment=LAYPA_OUTPUT_BASE_PATH=/home/eric/code/loghi/webservice-data/laypa-output
Environment=STORAGE_LOCATION=/home/eric/code/loghi/webservice-data/storage
Environment=HTR_OUTPUT_PATH=/home/eric/code/loghi/webservice-data/htr-output
Environment=WORK_DIR=/home/eric/code/loghi/webservice-data/orchestrator-work
ExecStart=/home/eric/code/loghi/webservice/orchestrator/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8090
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

The four `Environment=` paths must match whatever is set in `docker-compose.yml` for the `laypa`, `loghi-tooling`, and `htr` services — `WORK_DIR` is only used by the orchestrator itself for staging uploaded images.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now loghi-orchestrator.service
```

### Running it

The `laypa`, `htr`, and `loghi-tooling` containers still need to be up (`docker compose up -d` from the `webservice/` directory) — the orchestrator returns an error from `/transcribe` if they're unreachable, but it doesn't start them for you. Note these containers currently have `restart: always` in `docker-compose.yml`, so once started, Docker itself will bring them back up after a reboot too.

Useful commands:

```bash
sudo systemctl status loghi-orchestrator   # check it's running
sudo systemctl restart loghi-orchestrator  # restart after editing app.py
journalctl -u loghi-orchestrator -f        # tail logs
sudo systemctl stop loghi-orchestrator     # stop it
```

If you'd rather run it ad hoc instead of via systemd (e.g. for local debugging), you can still start it directly:

```bash
LAYPA_OUTPUT_BASE_PATH=/home/eric/code/loghi/webservice-data/laypa-output \
STORAGE_LOCATION=/home/eric/code/loghi/webservice-data/storage \
HTR_OUTPUT_PATH=/home/eric/code/loghi/webservice-data/htr-output \
WORK_DIR=/home/eric/code/loghi/webservice-data/orchestrator-work \
./venv/bin/uvicorn app:app --host 0.0.0.0 --port 8090
```
Stop the systemd-managed instance first (`sudo systemctl stop loghi-orchestrator`) to avoid a port conflict, and avoid a broad `pkill -f "uvicorn app:app"` when doing so — that pattern also matches the `htr` container's process from the host's process list.

### Using it

Other hosts on the local network can reach it at `http://<this-host-LAN-IP>:8090` (make sure your firewall allows the port from your subnet, e.g. `ufw allow from 192.168.2.0/24 to any port 8090`).

```bash
# Health check - confirms laypa/htr/loghi-tooling are all reachable
curl http://localhost:8090/health

# Transcribe a page, get back plain text
curl -X POST http://<host>:8090/transcribe \
  -F "image=@page.jpg" \
  -F "output_format=text"

# Transcribe a page, get back the full PageXML
curl -X POST http://<host>:8090/transcribe \
  -F "image=@page.jpg" \
  -F "output_format=xml" \
  -o page.xml
```

Optional form fields on `/transcribe`:

| Field | Default | Description |
|---|---|---|
| `model` | `baseline2` | Laypa model subdirectory name (under `LAYPA_MODEL_BASE_PATH`) |
| `recalculate_reading_order` | `true` | Run the reading-order recalculation step before returning |
| `split_words` | `false` | Also split text lines into words in the returned PageXML |
| `output_format` | `xml` | `xml` or `text` |
| `keep_intermediate` | `false` | Keep the per-request working directories instead of deleting them on completion — useful for debugging a failed transcription |

Each request uses a random per-request identifier, so concurrent requests from different callers don't collide.

## Note

- The web service setup provided here is adaptable and can be customized to fit specific project requirements.
- Ensure your Docker environment is properly configured, especially when leveraging GPU acceleration for processing tasks.

The flexibility and modularity of Loghi allow it to be tailored to a wide range of document analysis and text recognition projects, providing robust tools for researchers and developers alike.

