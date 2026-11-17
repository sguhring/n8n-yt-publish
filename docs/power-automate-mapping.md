# Same pattern in Microsoft Power Automate

The workflow is a generic **intake → enrich with AI → act → human approval → release → log** pattern. In a corporate Microsoft stack it maps like this:

| Step | n8n (this repo) | Power Automate equivalent |
|---|---|---|
| Intake | Local File Trigger on `inbox/<channel>/` | OneDrive / SharePoint "When a file is created in a folder" |
| Heavy lifting (transcription, image rendering) | Execute Command → Python script | Azure Function or Power Automate Desktop "Run Python script" |
| AI enrichment | Basic LLM Chain + local Ollama model + Structured Output Parser | AI Builder prompt ("Create text with GPT") or Azure OpenAI connector, then "Parse JSON" |
| Business rules (limits, footer, tags) | Code node (JavaScript) | Compose / expressions, or an Office Script |
| Act on the target system | YouTube node + HTTP Request (thumbnails.set) | Custom connector over the target API |
| Human in the loop | Telegram "send and wait for approval" | Approvals connector "Start and wait for an approval" (Teams / Outlook) |
| Branch on the decision | IF node | Condition |
| Failure alerting | Error workflow (Error Trigger → Telegram) | Scope + "Configure run after: has failed" → Teams / mail |
| Audit trail | Append to `logs/runs.csv` | "Add a row into a table" (Excel) or a SharePoint list |

What carries over unchanged: idempotent file intake, structured (schema-validated) LLM output instead of free text, nothing irreversible before a human approves, and every run logged with its outcome.
