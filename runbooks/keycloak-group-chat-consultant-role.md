# Keycloak realm role `group-chat-consultant`

[ORISO-UserService#1386](https://github.com/OpenResilienceInitiative/ORISO-UserService/issues/1386). When a counsellor is created with group chats enabled, UserService assigns the realm roles `consultant` and `group-chat-consultant`. If the second role does not exist, the assignment fails, the create saga deletes the new user again, and the Admin UI shows HTTP 424 (`UPDATE_USER_ROLES_IN_KEYCLOAK`).

`realm.json` is imported only when the realm is absent. It now contains the role, so a fresh or re-created realm has it. No Helm hook creates human realm roles, so an existing realm (Dev, Staging) needs the one-time step below.

No permission change is needed. The UserService admin identity already assigns `consultant` with `realm-management` `manage-users` and `view-realm`. `group-chat-consultant` is the same kind of plain, non-composite realm role. No client is added. The role is not a default role and no user gets it in this step.

## Add the role to an existing realm

The step is idempotent: it only creates the role when it is missing.

```sh
kubectl -n <ns> exec -it deploy/keycloak -- bash -ec '
  export KC_CLI_PASSWORD="$KEYCLOAK_ADMIN_PASSWORD"
  KC=/opt/keycloak/bin/kcadm.sh
  AUTH="--no-config --server http://localhost:8080/auth --realm master --user $KEYCLOAK_ADMIN"
  $KC get roles/group-chat-consultant -r online-beratung $AUTH >/dev/null 2>&1 \
    || $KC create roles -r online-beratung $AUTH -s name=group-chat-consultant -s composite=false \
         -s "description=Consultant who may create, start, stop and update group chats; assigned by UserService together with consultant"
  $KC get roles/group-chat-consultant -r online-beratung $AUTH --fields name,composite,clientRole'
```

Expected read-back: `"name" : "group-chat-consultant"`, `"composite" : false`, `"clientRole" : false`.

Then create a counsellor with group chats enabled in the Admin UI. The request succeeds, and the user has both roles.

## Recovery

Do this only if you must roll back. Deleting the role also removes it from every user who has it. Counsellors created with group chats then lose the right to create, start, stop and update group chats.

```sh
kubectl -n <ns> exec -it deploy/keycloak -- bash -ec '
  export KC_CLI_PASSWORD="$KEYCLOAK_ADMIN_PASSWORD"
  KC=/opt/keycloak/bin/kcadm.sh
  AUTH="--no-config --server http://localhost:8080/auth --realm master --user $KEYCLOAK_ADMIN"
  $KC delete roles/group-chat-consultant -r online-beratung $AUTH'
```
