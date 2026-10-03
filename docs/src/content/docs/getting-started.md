---
title: Getting started
description: Install Notosaurus in Anki, connect your phone and make your first deck from a photo.
---

Notosaurus is an add-on for Anki desktop. It runs on your computer, and you use it from
your phone's browser, on the same Wi-Fi.

## What you need

- **Anki** on a computer (Windows, macOS or Linux), a recent version.
- **A phone** on the same Wi-Fi as the computer.
- **A key for an AI service.** Gemini is the cheapest and has a free tier to try
  Notosaurus; Claude reads handwriting best. A lesson costs from a fraction of a cent to a
  few cents.

:::caution[Free tiers and privacy]
Free tiers usually let the AI provider use what you send to improve their models. For your
children's notebooks, prefer a paid key: a few euros last a long time.
:::

## 1. Install the add-on

1. Download `notosaurus-<version>.ankiaddon` from the
   [latest release](https://github.com/cchabanois/notosaurus/releases/latest).
2. Double-click the file (or in Anki: **Tools → Add-ons → Install from file**), then
   restart Anki.
3. On first start, Notosaurus asks before installing its components (about 300 MB). It
   takes a few minutes, once.

## 2. Add your AI key

In Anki, open **Tools → Notosaurus → Settings**, choose the **AI service**, paste your key
and click **Test**. Settings only open on the computer: children can't change them from a
phone.

![Settings: choosing the AI service](../../assets/screenshots/en/settings-ai.png)

## 3. Open Notosaurus on the phone

In Anki, choose **Tools → Notosaurus → Open on the phone…** and scan the QR code with the
phone's camera. Then add the page to the home screen: it opens like an app.

![Settings: the QR code to open Notosaurus on the phone](../../assets/screenshots/en/settings-phones.png)

:::tip
Only devices that scanned this QR code can use Notosaurus. If a phone is lost or lent,
**Settings → Phones → Disconnect every phone**.
:::

## 4. Your first lesson

1. Under **Lesson photos**, take a photo of the page (or several pages).
2. Under **Instructions**, pick what to make: vocabulary, questions, fill in the blanks, a
   diagram to complete… **Automatic** chooses from the lesson.

   ![A photo of the lesson and the instructions](../../assets/screenshots/en/instructions.png)

3. Tap **Generate cards** and wait a few seconds.
4. Check the cards. Edit them, delete some, or ask the AI to fix them in plain words.

   ![The cards, ready to review](../../assets/screenshots/en/review.png)

   ![Asking the AI for a correction](../../assets/screenshots/en/correction.png)

5. Tap **Add to Anki**.

The lesson is saved: you can reopen it later from the list of lessons, correct it and send it
again. Its cards are updated in Anki, not duplicated.

With the **Diagram to complete** instructions, the labels of a diagram are hidden behind
numbers: each card asks for one of them.

![A diagram with its labels hidden](../../assets/screenshots/en/diagram.png)

## 5. Review on the phone

To review on the phone, use **AnkiDroid** (Android) or **AnkiMobile** (iPhone) with an
**AnkiWeb** account: Notosaurus syncs Anki after sending the cards. With several children,
see [Several children](../several-children/).
