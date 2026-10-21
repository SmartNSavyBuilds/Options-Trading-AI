# Security

## Scope

This project handles broker credentials, market data, and optional outbound notifications. Keep secrets out of Git and treat every credential as sensitive.

## Local setup

- Copy `.env.example` to `.env` and fill in only the values you need.
- Do not commit `.env`, generated `outputs/`, or local runtime data.
- Use paper-trading credentials only. Do not configure live brokerage keys.

## Reporting

Do not open public issues with secrets, account details, order IDs tied to private accounts, or webhook URLs. Rotate any credential that may have been exposed.
