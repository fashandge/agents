# Phoenix weather lookup — worker kickoff

## Objective

Get the current weather in Phoenix, Arizona for the user and return a concise, factual report.

## Instructions

- Treat “Phoenix” as Phoenix, Arizona, United States.
- Use a current, authoritative weather source or weather tool; do not rely on model memory.
- Report the observation time (including time zone when supplied), conditions, temperature, feels-like temperature if available, wind, and a short today forecast (high/low and precipitation chance if available).
- State the source used and include a direct link when available.
- Keep the final response brief and user-facing.

## Scope and constraints

- This is a lookup-only task. Do not modify repository files other than protocol artifacts, install packages, commit, push, or change external state.
- If the source cannot provide an item, say so plainly rather than guessing.

## Done condition

Emit the weather report as the task result with source-backed, current values for Phoenix, AZ.
