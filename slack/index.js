import { App } from "@slack/bolt";

const app = new App({
	  token: process.env.SLACK_BOT_TOKEN,
	  appToken: process.env.SLACK_APP_TOKEN,
	  socketMode: true,
});

app.event("app_mention", async ({ event, say }) => {
	  await say(`收到: ${event.text}`);
});

(async () => {
	  await app.start();
	  console.log("Slack bot running");
})();
