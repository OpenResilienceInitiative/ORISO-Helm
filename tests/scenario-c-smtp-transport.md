# Scenario C SMTP transport

The normal Job and CronJob keep the existing transport in legacy mode. Explicit five-account mode uses the single MAIL credential to read the current saved CTS snapshot, apply it through the bounded SMTP receiver, and acknowledge that exact revision. An unavailable or conflicting write cannot clear CTS pending state.

The MAIL bearer has exactly five audiences: tenantservice, agencyservice, consultingtypeservice, userservice and oriso-task-commands. The signed command proof uses oriso-smtp-control separately. Its existing fifteen fields are unchanged. The context key contains at least 32 decoded bytes and differs from the MAIL client secret. The actual internal realm ID comes from installer configuration; the MAIL process never queries privileged realm metadata. TASK_IDENTITY_ISSUER pins the public issuer independently of the internal Keycloak transport URL.

After an unknown write reply, the helper retries the identical body and revision with a fresh proof. It acknowledges only a known matching success. A status GET returning the same revision does not establish which body was written, so it cannot resolve an unknown write. Unsupported acknowledgements fail the C run. The existing legacy upgrade warning remains unchanged.

**For developers — public process/HTTP regression tests; the local APIs and JWT are synthetic:**

```bash
python3 tests/test_scenario_c_smtp_transport.py
python3 tests/test_keycloak_reconcile_smtp.py
```

The joined native gate builds the selected SMTP module, starts its disposable pinned Keycloak and bounded Mongo fixture, provisions five synthetic actor accounts and starts the real selected CTS application with an encrypted saved snapshot. The actual User broker obtains and verifies the native MAIL JWT. That same bearer goes through stdin to the production Python transport and reaches CTS snapshot, SMTP apply and CTS acknowledgement. The test server checks durable CTS state and the fixture installer checks actual SMTP state. No token is written to a file or command argument. Fixture secrets live only in a private temporary directory that the runner removes.

**For developers — acquire the shared heavy-test lease first, then supply actual checked source trees and Java21:**

```bash
python3 tests/scenario_c_mail_native.py \
  --keycloak /local/worktrees/ORISO-Keycloak \
  --cts /local/worktrees/ORISO-ConsultingTypeService \
  --user /local/worktrees/ORISO-UserService \
  --java-home /local/jdk21-home \
  --evidence /local/artifacts/native-mail
```

The selected CTS tree must contain the test-only ScenarioCMailNativeServer and the User tree must contain RealMailActorNativeGate. Missing fixtures, absent provider jars, nonzero or skipped cases, stale reports, changed source inputs and failed exact acknowledgements fail the gate. The runner archives the complete four-case XML before another Maven gate can overwrite it. Source manifests and provider/image digests identify what was tested. This is local native integration proof; production packaging, deployment and received real mail remain separate acceptance.
