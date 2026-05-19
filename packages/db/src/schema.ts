import { sqliteTable, text, integer, real } from "drizzle-orm/sqlite-core";
import { sql } from "drizzle-orm";

export const races = sqliteTable("races", {
  id: text("id").primaryKey(),
  date: text("date").notNull(),
  venue: text("venue").notNull(),
  distance: integer("distance").notNull(),
  trackCondition: text("track_condition").notNull(),
  lapTimes: text("lap_times"),
  resultConfirmed: integer("result_confirmed", { mode: "boolean" }).default(false),
  createdAt: text("created_at").default(sql`(datetime('now'))`),
});

export const horses = sqliteTable("horses", {
  id: text("id").primaryKey(),
  name: text("name").notNull(),
  sire: text("sire"),
  damsire: text("damsire"),
  createdAt: text("created_at").default(sql`(datetime('now'))`),
});

export const pastResults = sqliteTable("past_results", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  horseId: text("horse_id").notNull().references(() => horses.id),
  raceDate: text("race_date").notNull(),
  finishTime: real("finish_time"),
  passageRank: text("passage_rank"),
  last3Furlong: real("last_3furlong"),
  raceComment: text("race_comment"),
  structuredComment: text("structured_comment"),
  createdAt: text("created_at").default(sql`(datetime('now'))`),
});

export const predictions = sqliteTable("predictions", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  raceId: text("race_id").notNull().references(() => races.id),
  horseId: text("horse_id").notNull().references(() => horses.id),
  winProbability: real("win_probability").notNull(),
  reasoningLogic: text("reasoning_logic"),
  oddsAtPrediction: real("odds_at_prediction"),
  expectedValue: real("expected_value"),
  modelName: text("model_name").notNull(),
  recommended: integer("recommended", { mode: "boolean" }).default(false),
  createdAt: text("created_at").default(sql`(datetime('now'))`),
});

export const actualResults = sqliteTable("actual_results", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  raceId: text("race_id").notNull().references(() => races.id),
  horseId: text("horse_id").notNull().references(() => horses.id),
  finishOrder: integer("finish_order"),
  confirmedOdds: real("confirmed_odds"),
  hit: integer("hit", { mode: "boolean" }).default(false),
  brierScore: real("brier_score"),
  createdAt: text("created_at").default(sql`(datetime('now'))`),
});
