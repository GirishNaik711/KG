import NextAuth from "next-auth";

const authEnabled = process.env.AUTH_ENABLED === "true";
const appEnvironment = process.env.APP_ENV ?? "dev";
const issuerByEnvironment: Record<string, string | undefined> = {
  dev: process.env.AUTH_ISSUER_DEV,
  stage: process.env.AUTH_ISSUER_STAGE,
  prod: process.env.AUTH_ISSUER_PROD,
};

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [
    {
      id: "ping",
      name: "PING Identity",
      type: "oidc",
      issuer: issuerByEnvironment[appEnvironment],
      clientId: process.env.AUTH_CLIENT_ID,
      clientSecret: process.env.AUTH_CLIENT_SECRET,
    },
  ],
  secret: process.env.AUTH_SECRET,
  trustHost: true,
  pages: { signIn: "/api/auth/signin" },
  callbacks: {
    authorized: ({ auth: session }) => !authEnabled || Boolean(session?.user),
  },
});
