# XLSX-only derivative of the observed Linux amd64 Hermes terminal sandbox.
FROM nousresearch/hermes-sandbox@sha256:c7d8607d076f26a64e0bd6828f74e1764af43fe3281368188459bb693bfa7567
COPY sandbox-xlsx.requirements.txt /opt/hermes-xlsx-requirements.txt
RUN python3 -m venv /opt/hermes-xlsx && \
    /opt/hermes-xlsx/bin/python -m pip --isolated install --disable-pip-version-check \
        --no-cache-dir --require-hashes --only-binary=:all: \
        -r /opt/hermes-xlsx-requirements.txt && \
    /opt/hermes-xlsx/bin/python -m pip check
