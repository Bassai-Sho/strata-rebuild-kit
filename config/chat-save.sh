#!/usr/bin/env bash
name="${1:?usage: chat-save.sh NAME}"
curl -s -X POST "http://127.0.0.1:8085/slots/0?action=save" -H "Content-Type: application/json" -d "{\"filename\":\"${name}.bin\"}"; echo
ls -lh ~/github/strata-work/sessions | tail -n +2
