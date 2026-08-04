# Mobile operator app

A Flutter app for operators: watch conversations live, take over from the bot,
reply by hand, review customers and analytics. It is a **second client for the
same `/admin` API** the React dashboard uses ([docs/DASHBOARD.md](DASHBOARD.md)),
not a separate backend. Anything the app can do, the API already did.

The project lives in `mobile/`.

---

## Read this first: the checkout is not runnable as-is

Three things are deliberately absent from git, and each one stops a plain
`flutter run`. Section ["How to run"](#how-to-run) handles all three, in order.

1. **There are no platform folders.** `mobile/` contains only
   `lib/`, `pubspec.yaml`, `l10n.yaml` and `analysis_options.yaml`. There is no
   `android/`, `ios/`, `web/`, `macos/`, `linux/` or `windows/` directory, so
   there is nothing to build *into*. You generate them once with
   `flutter create`.
2. **No generated Dart is committed.** The app depends on `freezed`,
   `json_serializable` and `riverpod_generator`, but no `*.freezed.dart` or
   `*.g.dart` file is in the repository. Until `build_runner` has run, the
   analyser reports missing parts in every model file.
3. **Firebase is a dependency that is never initialised.** `firebase_core` and
   `firebase_messaging` are in `pubspec.yaml`, but `lib/main.dart` does **not**
   call `Firebase.initializeApp()`. Push notifications are therefore not wired
   up end to end. The app runs fine without them — the live stream is the
   WebSocket, not FCM — but do not expect background push until someone adds
   the platform config and the init call. See
   ["Push notifications"](#push-notifications-not-wired-up).

---

## What the app talks to

| | |
| --- | --- |
| Base URL | Typed in by the operator on the login screen and stored on the device. `AppConfig.defaultBaseUrl` is the placeholder `https://api.example.com` and is meant to be replaced, not used. |
| API prefix | `/admin` — `DioClient.updateBaseUrl` appends it, so every path in `ApiEndpoints` is relative to it (`/conversations`, `/stats`, ...). |
| Auth | `X-API-Key: <ADMIN_API_KEY>` added to every request by `AuthInterceptor`. |
| Live stream | `/ws/events`, with the scheme swapped to `ws`/`wss` by `wsUrlFromHttp`. |

There is **no login endpoint and no per-operator account.** "Signing in" means
storing the one shared `ADMIN_API_KEY` on the phone. Every operator using the
app is indistinguishable from every other one, and from the dashboard, in the
backend's eyes. Treat the key as a production credential: anyone holding it can
read every transcript and send messages as the company.

### Timeouts and paging (`lib/core/config/app_config.dart`)

| Setting | Value |
| --- | --- |
| Connect timeout | 10 s |
| Receive timeout | 15 s |
| Send timeout | 10 s |
| WebSocket reconnect backoff | 1 s → 30 s |
| Page size | 50 |

`Dio` is configured with `validateStatus: (s) => s < 500`, so 4xx responses come
back as normal responses and are turned into typed failures by
`ErrorInterceptor`: `TimeoutFailure`, `OfflineFailure`, `UnauthorizedFailure`
(401), `NotFoundFailure` (404), `ServerFailure`, `UnknownFailure`. FastAPI's
`detail` field is used as the message when present.

---

## Screens

Routing is `go_router` (`lib/core/router/app_router.dart`). `/splash` and
`/login` are full-screen; everything else sits inside a `ShellRoute` with a
five-tab bottom bar. An unauthenticated user is redirected to `/login`; an
authenticated one is bounced off `/login` to `/chats`.

| Route | Screen |
| --- | --- |
| `/splash` | Startup, decides where to go |
| `/login` | Base URL + admin key |
| `/chats` | Conversation list (**Chats** tab) |
| `/chats/:id` | Transcript, take over / resume AI, manual reply |
| `/customers` | Customer list (**Customers** tab) |
| `/customers/:waId` | One customer and their earlier sessions |
| `/analytics` | KPIs, spend, usage (**Analytics** tab) |
| `/notifications` | Alerts (**Alerts** tab) |
| `/settings` | Base URL, key, theme, language, operator name (**Settings** tab) |

### A conversation is a session, not a customer

This is the single most common misreading of the list. Sessions close after a
period of silence and a returning customer opens a new one, so **the same phone
number appears many times** in `/chats` over time. Sessions are never merged.
`/customers/:waId` is how you move between one person's sessions;
`ApiEndpoints.conversationHistory` returns navigation between them, not a
combined transcript. Full rules in
[docs/SESSION_LIFECYCLE.md](SESSION_LIFECYCLE.md).

---

## Storage

`lib/core/storage/secure_storage.dart` wraps `flutter_secure_storage`
(Android `encryptedSharedPreferences`, iOS keychain
`first_unlock`) with five keys: `admin_api_key`, `base_url`, `operator_name`,
`theme_mode`, `locale`.

`operator_name` is **local only**. It labels replies in the app's own UI; it is
not sent to the backend and does not appear on the message in the database.
Attribution is a real gap, not a display bug.

`main.dart` also opens two Hive boxes, `cache` and `settings`, before
`runApp`, and preloads the key and base URL so the splash screen can decide
immediately whether a session exists.

---

## Localisation

English and Arabic, from `lib/l10n/app_en.arb` and `app_ar.arb`. `l10n.yaml`
points at `lib/l10n` with `app_en.arb` as the template.

**Do not run `flutter gen-l10n` expecting it to be a no-op.**
`lib/l10n/app_localizations.dart` is committed and imported directly by
`lib/app.dart`, while `pubspec.yaml` also sets `generate: true`. Regenerating
into the same directory can overwrite or duplicate that file. If you change an
`.arb`, update `app_localizations.dart` in the same commit and check the app
still compiles against the committed file.

---

## How to run

Flutter SDK **3.22 or newer**, Dart SDK **3.4+** (`environment` in
`pubspec.yaml`). Check with `flutter --version`.

### 1. Generate the platform folders (once)

From the repository root:

```bash
cd mobile
flutter create --platforms=android,ios --org com.alkayaneg --project-name whatsapp_ai_mobile .
```

Running `flutter create` in a directory that already has `lib/` and
`pubspec.yaml` adds the missing platform folders and leaves your Dart code
alone. Add `,web` if you want a browser build too.

> Verify afterwards that `pubspec.yaml` still reads `generate: true` and still
> has every dependency. If `flutter create` touched it, restore it with
> `git checkout pubspec.yaml`.

### 2. Install dependencies

```bash
flutter pub get
```

### 3. Generate the Dart that is not committed

```bash
dart run build_runner build --delete-conflicting-outputs
```

This is **not optional.** Skip it and the analyser fails on every `freezed`
model. Re-run it after any change to a model annotated with `@freezed`,
`@JsonSerializable` or a Riverpod generator annotation.

### 4. Point the app at a backend

The URL is entered at runtime on the login screen — there is nothing to edit in
the source. What to type depends on where the app is running:

| Where the app runs | Base URL |
| --- | --- |
| Android emulator, backend on your machine | `http://10.0.2.2:8000` |
| iOS simulator, backend on your machine | `http://localhost:8000` |
| Physical phone, backend on your machine | `http://<your-LAN-IP>:8000` |
| Production | `https://<your-domain>` |

Enter the URL **without** `/admin` — `DioClient` appends it.

Two traps with local development:

- **Android blocks cleartext HTTP by default.** A physical Android device on
  `http://…` will fail with a connection error until you allow cleartext for
  that host in the generated `android/app/src/main/AndroidManifest.xml`. The
  emulator via `10.0.2.2` is the easier path. Never allow cleartext in a
  release build.
- **In production the app container binds `127.0.0.1:8000`** and only nginx is
  public (`docker-compose.prod.yml`). A phone cannot reach port 8000 directly;
  use the HTTPS domain.

### 5. Run

```bash
flutter devices          # confirm a target is attached
flutter run              # debug
flutter run --release    # release-mode smoke test
```

On first launch: enter the base URL and `ADMIN_API_KEY` on the login screen.
Both are written to secure storage, so subsequent launches go straight to
`/chats`.

### 6. Build artifacts

```bash
flutter build apk --release          # Android, sideloadable
flutter build appbundle --release    # Android, Play Store
flutter build ipa --release          # iOS, needs macOS + Xcode + signing
```

Neither store listing nor signing config is in this repository. Android release
builds are unsigned until you add a keystore, and the iOS build needs an Apple
team configured in Xcode.

### Static analysis

```bash
flutter analyze
```

`analysis_options.yaml` enables `flutter_lints`; `custom_lint` and
`riverpod_lint` are dev dependencies, so `dart run custom_lint` also works.

### Tests

There are none. `mobile/` has no `test/` directory, and CI does not build or
analyse the mobile app — the pipeline covers the Python backend and the React
dashboard only. Every mobile change is verified by hand today.

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `No pubspec.yaml file found` | You are not in `mobile/`. |
| Errors about missing `*.freezed.dart` / `*.g.dart` parts | Step 3 was skipped. |
| `No supported devices` / nothing to build | Step 1 was skipped, or no emulator is running. |
| Login rejected with 401 | The key does not match `ADMIN_API_KEY` on that backend. There is no user account to check — only that one string. |
| Everything times out on an emulator | `localhost` inside an Android emulator is the emulator. Use `10.0.2.2`. |
| Connection error on a physical Android device over `http://` | Cleartext is blocked; see step 4. |
| Requests 404 | The base URL was entered *with* `/admin`, so paths resolve to `/admin/admin/...`. |
| Live updates never arrive, REST works | `/ws/events` is not reachable — a proxy that does not forward WebSocket upgrades is the usual cause. nginx in this repo forwards it; other proxies may not. |
| Interactive menu selections look like plain text | Expected. Inbound rows of type `interactive` are not rendered specially yet; only the label the customer tapped is shown. |

---

## Push notifications (not wired up)

To finish this you need all of:

1. A Firebase project, with `google-services.json` in `android/app/` and
   `GoogleService-Info.plist` in the iOS runner.
2. `Firebase.initializeApp()` in `lib/main.dart` — it is absent today.
3. A backend that stores device tokens and sends to FCM. **No such endpoint
   exists.** `/admin` has nothing for device registration, so this is backend
   work, not just app work.

Until then, `flutter_local_notifications` can still raise a local alert while
the app is running and the WebSocket is connected, but a closed app is silent.

---

## Known limitations

- **One shared key, no operator identity.** Every action is anonymous in the
  database. This is the main blocker for multi-operator use.
- **No offline write queue.** Hive caches reads; a reply attempted while
  offline fails rather than queueing.
- **No media.** Inbound images and documents are recorded by the backend but
  never fetched, so the app cannot display them.
- **`interactive` messages render as plain text**, as noted above.
- **`LogInterceptor` prints request metadata with `print`.** Headers are not
  logged, so the API key does not leak, but debug output in a release build is
  still noise worth removing.
- **No CI, no tests** for this package.
