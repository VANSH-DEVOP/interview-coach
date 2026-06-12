import Link from "next/link";
import { BarChart3, FileText, MessageSquare, ShieldCheck } from "lucide-react";

import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

const FEATURES = [
  {
    icon: MessageSquare,
    title: "Adaptive AI Interviews",
    description:
      "Practice with AI-driven interviews that adapt follow-up questions to your answers.",
  },
  {
    icon: FileText,
    title: "Resume-Aware Questions",
    description:
      "Upload your resume and receive questions grounded in your actual experience.",
  },
  {
    icon: BarChart3,
    title: "Evaluation Reports",
    description:
      "Receive structured feedback with scores, strengths, and areas to improve.",
  },
  {
    icon: ShieldCheck,
    title: "Progress Tracking",
    description:
      "Track interview history and measure improvement across sessions over time.",
  },
] as const;

export default function LandingPage() {
  return (
    <div className="flex min-h-screen flex-col">
      {/* Top navigation */}
      <header className="border-b">
        <div className="container flex h-16 items-center justify-between">
          <span className="text-base font-semibold">
            InterviewPilot <span className="text-primary">AI</span>
          </span>
          <nav className="flex items-center gap-2" aria-label="Authentication">
            <Link href="/login" className={cn(buttonVariants({ variant: "ghost" }))}>
              Sign in
            </Link>
            <Link href="/register" className={cn(buttonVariants())}>
              Get started
            </Link>
          </nav>
        </div>
      </header>

      {/* Hero */}
      <main className="flex-1">
        <section className="container py-20 sm:py-28">
          <div className="mx-auto max-w-3xl text-center">
            <h1 className="text-4xl font-semibold tracking-tight sm:text-5xl">
              Prepare for interviews with an <span className="text-primary">AI copilot</span>
            </h1>
            <p className="mx-auto mt-6 max-w-2xl text-base text-muted-foreground sm:text-lg">
              InterviewPilot AI runs realistic, adaptive mock interviews based on your resume,
              then delivers structured evaluation reports so you know exactly where you stand.
            </p>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <Link href="/register" className={cn(buttonVariants({ size: "lg" }))}>
                Start preparing
              </Link>
              <Link
                href="/login"
                className={cn(buttonVariants({ size: "lg", variant: "outline" }))}
              >
                Sign in to your account
              </Link>
            </div>
          </div>
        </section>

        {/* Features */}
        <section className="border-t bg-card/50">
          <div className="container py-16 sm:py-20">
            <h2 className="text-center text-2xl font-semibold tracking-tight">
              Built for serious preparation
            </h2>
            <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {FEATURES.map(({ icon: Icon, title, description }) => (
                <Card key={title}>
                  <CardHeader>
                    <Icon className="h-6 w-6 text-primary" aria-hidden />
                    <CardTitle className="pt-2 text-base">{title}</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <p className="text-sm text-muted-foreground">{description}</p>
                  </CardContent>
                </Card>
              ))}
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t">
        <div className="container flex h-16 items-center justify-between text-sm text-muted-foreground">
          <span>InterviewPilot AI</span>
          <span>Enterprise-grade interview preparation</span>
        </div>
      </footer>
    </div>
  );
}
