"use client";

import { signIn, signOut, useSession } from "next-auth/react";

export function AuthStatus() {
  const { data: session } = useSession();

  if (!session?.user) {
    return <button className="auth-button" onClick={() => signIn("ping")}>Sign in</button>;
  }

  return <button className="auth-button" onClick={() => signOut()}>Sign out {session.user.name ?? session.user.email}</button>;
}
