-- knowledge-kb 空白完整库（云平台网页 SQL 控制台版）
-- 生成日期：2026-07-24
-- 文件自带事务；执行环境需支持一次运行整份脚本。
-- 需要 PostgreSQL 16 兼容数据库，并将 pgvector 安装在 public schema。

BEGIN;
--
-- PostgreSQL database dump
--


-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: knowledgestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.knowledgestatus AS ENUM (
    'DRAFT',
    'REVIEW',
    'PUBLISHED',
    'DEPRECATED'
);


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: categories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.categories (
    id character varying(64) NOT NULL,
    name character varying(128) NOT NULL,
    parent_id character varying(64),
    level integer,
    sort_order integer,
    created_at timestamp without time zone
);


--
-- Name: integration_ingestions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.integration_ingestions (
    id character varying(64) NOT NULL,
    event_id character varying(128) NOT NULL,
    idempotency_key character varying(128) NOT NULL,
    source_system character varying(64) NOT NULL,
    source_conversation_id character varying(128) NOT NULL,
    source_conversation_url character varying(1024),
    source_message_ids json,
    redaction_status character varying(32) NOT NULL,
    processing_metadata json,
    selection_metadata json,
    status character varying(32) NOT NULL,
    knowledge_id character varying(64),
    error_code character varying(64),
    error_message character varying(512),
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    candidate_payload json,
    review_metadata json,
    review_status character varying(32),
    reviewed_by character varying(128),
    reviewed_at timestamp without time zone,
    submitted_at timestamp without time zone
);


--
-- Name: knowledge_change_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_change_logs (
    id character varying(64) NOT NULL,
    knowledge_id character varying(64) NOT NULL,
    changed_by character varying(128) NOT NULL,
    changed_fields json NOT NULL,
    before_data json NOT NULL,
    after_data json NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: knowledge_deduplication_feedback; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_deduplication_feedback (
    id character varying(64) NOT NULL,
    knowledge_id character varying(64) NOT NULL,
    matched_knowledge_id character varying(64) NOT NULL,
    verdict character varying(32) NOT NULL,
    reason text NOT NULL,
    submitted_by character varying(128) NOT NULL,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: knowledge_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_embeddings (
    id character varying(64) NOT NULL,
    knowledge_id character varying(64) NOT NULL,
    embedding_model character varying(256) NOT NULL,
    embedding_dimension integer NOT NULL,
    content_hash character varying(64) NOT NULL,
    embedding json NOT NULL,
    embedding_vector public.vector(1024),
    title_embedding_vector public.vector(1024),
    content_embedding_vector public.vector(1024),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: knowledge_item_number_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.knowledge_item_number_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: knowledge_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_items (
    id character varying(64) NOT NULL,
    title character varying(256) NOT NULL,
    subtitles json,
    content json NOT NULL,
    category_id character varying(64) NOT NULL,
    status public.knowledgestatus,
    source character varying(32),
    source_session_id character varying(128),
    quality_score double precision,
    applicable_scenes json,
    applicable_categories json,
    applicable_brands json,
    applicable_models json,
    deduplication_metadata json,
    created_by character varying(128) NOT NULL,
    updated_by character varying(128),
    updated_at timestamp without time zone,
    created_at timestamp without time zone
);


--
-- Name: knowledge_media; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_media (
    id character varying(64) NOT NULL,
    knowledge_id character varying(64) NOT NULL,
    media_type character varying(16) NOT NULL,
    filename character varying(256) NOT NULL,
    original_name character varying(256) NOT NULL,
    file_path character varying(512) NOT NULL,
    file_size integer,
    mime_type character varying(128),
    alt character varying(256),
    caption text,
    duration character varying(32),
    sort_order integer,
    created_at timestamp without time zone
);


--
-- Name: knowledge_search_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_search_embeddings (
    id character varying(64) NOT NULL,
    knowledge_id character varying(64) NOT NULL,
    embedding_model character varying(256) NOT NULL,
    embedding_kind character varying(32) NOT NULL,
    chunk_index integer NOT NULL,
    content_hash character varying(64) NOT NULL,
    source_text text NOT NULL,
    embedding_dimension integer NOT NULL,
    embedding json NOT NULL,
    embedding_vector public.vector(1024),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: knowledge_tags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_tags (
    id character varying(64) NOT NULL,
    knowledge_id character varying(64) NOT NULL,
    tag_value_id character varying(64) NOT NULL,
    created_at timestamp without time zone
);


--
-- Name: media_deletion_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.media_deletion_tasks (
    id character varying(64) NOT NULL,
    storage_backend character varying(16) NOT NULL,
    storage_key character varying(512) NOT NULL,
    filename character varying(256) NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp without time zone DEFAULT now() NOT NULL,
    last_error text DEFAULT ''::text NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
);


--
-- Name: media_upload_staging; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.media_upload_staging (
    id character varying(64) NOT NULL,
    username character varying(128) NOT NULL,
    storage_backend character varying(16) NOT NULL,
    storage_key character varying(512) NOT NULL,
    filename character varying(256) NOT NULL,
    status character varying(16) DEFAULT 'uploading'::character varying NOT NULL,
    media_type character varying(16) NOT NULL,
    original_name character varying(256) NOT NULL,
    file_size integer DEFAULT 0 NOT NULL,
    mime_type character varying(128) DEFAULT 'image/png'::character varying NOT NULL,
    alt character varying(256) DEFAULT ''::character varying NOT NULL,
    caption text DEFAULT ''::text NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_media_upload_staging_status CHECK (((status)::text = ANY ((ARRAY['uploading'::character varying, 'ready'::character varying])::text[])))
);


--
-- Name: retrieval_quality_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.retrieval_quality_events (
    id character varying(64) NOT NULL,
    idempotency_key character varying(128) NOT NULL,
    source_system character varying(64) NOT NULL,
    conversation_id character varying(128),
    query_text character varying(1000) NOT NULL,
    candidate_count integer NOT NULL,
    top_knowledge_id character varying(64),
    top_rerank_score double precision,
    score_threshold double precision NOT NULL,
    selected boolean NOT NULL,
    outcome character varying(32) NOT NULL,
    event_metadata json,
    created_at timestamp without time zone
);


--
-- Name: tag_dimensions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tag_dimensions (
    id character varying(64) NOT NULL,
    name character varying(64) NOT NULL,
    created_at timestamp without time zone
);


--
-- Name: tag_values; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tag_values (
    id character varying(64) NOT NULL,
    dimension_id character varying(64) NOT NULL,
    value character varying(128) NOT NULL,
    created_at timestamp without time zone
);


--
-- Name: usage_stats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_stats (
    id character varying(64) NOT NULL,
    knowledge_id character varying(64) NOT NULL,
    recommend_count integer,
    click_count integer,
    feedback_score double precision,
    last_used_at timestamp without time zone
);


--
-- Name: user_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_sessions (
    token_hash character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id character varying(64) NOT NULL,
    username character varying(64) NOT NULL,
    password_hash character varying(256) NOT NULL,
    role character varying(32) NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: categories categories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT categories_pkey PRIMARY KEY (id);


--
-- Name: integration_ingestions integration_ingestions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_ingestions
    ADD CONSTRAINT integration_ingestions_pkey PRIMARY KEY (id);


--
-- Name: knowledge_change_logs knowledge_change_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_change_logs
    ADD CONSTRAINT knowledge_change_logs_pkey PRIMARY KEY (id);


--
-- Name: knowledge_deduplication_feedback knowledge_deduplication_feedback_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_deduplication_feedback
    ADD CONSTRAINT knowledge_deduplication_feedback_pkey PRIMARY KEY (id);


--
-- Name: knowledge_embeddings knowledge_embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_embeddings
    ADD CONSTRAINT knowledge_embeddings_pkey PRIMARY KEY (id);


--
-- Name: knowledge_items knowledge_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_items
    ADD CONSTRAINT knowledge_items_pkey PRIMARY KEY (id);


--
-- Name: knowledge_media knowledge_media_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_media
    ADD CONSTRAINT knowledge_media_pkey PRIMARY KEY (id);


--
-- Name: knowledge_search_embeddings knowledge_search_embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_search_embeddings
    ADD CONSTRAINT knowledge_search_embeddings_pkey PRIMARY KEY (id);


--
-- Name: knowledge_tags knowledge_tags_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_tags
    ADD CONSTRAINT knowledge_tags_pkey PRIMARY KEY (id);


--
-- Name: media_deletion_tasks media_deletion_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_deletion_tasks
    ADD CONSTRAINT media_deletion_tasks_pkey PRIMARY KEY (id);


--
-- Name: media_upload_staging media_upload_staging_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_upload_staging
    ADD CONSTRAINT media_upload_staging_pkey PRIMARY KEY (id);


--
-- Name: retrieval_quality_events retrieval_quality_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.retrieval_quality_events
    ADD CONSTRAINT retrieval_quality_events_pkey PRIMARY KEY (id);


--
-- Name: tag_dimensions tag_dimensions_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_dimensions
    ADD CONSTRAINT tag_dimensions_name_key UNIQUE (name);


--
-- Name: tag_dimensions tag_dimensions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_dimensions
    ADD CONSTRAINT tag_dimensions_pkey PRIMARY KEY (id);


--
-- Name: tag_values tag_values_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_values
    ADD CONSTRAINT tag_values_pkey PRIMARY KEY (id);


--
-- Name: categories uq_category_name_parent; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT uq_category_name_parent UNIQUE (name, parent_id);


--
-- Name: knowledge_deduplication_feedback uq_dedup_feedback_submitter; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_deduplication_feedback
    ADD CONSTRAINT uq_dedup_feedback_submitter UNIQUE (knowledge_id, matched_knowledge_id, submitted_by);


--
-- Name: knowledge_embeddings uq_knowledge_embedding_model; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_embeddings
    ADD CONSTRAINT uq_knowledge_embedding_model UNIQUE (knowledge_id, embedding_model);


--
-- Name: knowledge_media uq_knowledge_media_filename; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_media
    ADD CONSTRAINT uq_knowledge_media_filename UNIQUE (filename);


--
-- Name: knowledge_search_embeddings uq_knowledge_search_embedding; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_search_embeddings
    ADD CONSTRAINT uq_knowledge_search_embedding UNIQUE (knowledge_id, embedding_model, embedding_kind, chunk_index);


--
-- Name: knowledge_tags uq_knowledge_tag; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_tags
    ADD CONSTRAINT uq_knowledge_tag UNIQUE (knowledge_id, tag_value_id);


--
-- Name: media_upload_staging uq_media_upload_staging_filename; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_upload_staging
    ADD CONSTRAINT uq_media_upload_staging_filename UNIQUE (filename);


--
-- Name: tag_values uq_tag_value_per_dim; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_values
    ADD CONSTRAINT uq_tag_value_per_dim UNIQUE (dimension_id, value);


--
-- Name: usage_stats usage_stats_knowledge_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_stats
    ADD CONSTRAINT usage_stats_knowledge_id_key UNIQUE (knowledge_id);


--
-- Name: usage_stats usage_stats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_stats
    ADD CONSTRAINT usage_stats_pkey PRIMARY KEY (id);


--
-- Name: user_sessions user_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_sessions
    ADD CONSTRAINT user_sessions_pkey PRIMARY KEY (token_hash);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: ix_categories_parent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_categories_parent_id ON public.categories USING btree (parent_id);


--
-- Name: ix_integration_ingestions_event_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_integration_ingestions_event_id ON public.integration_ingestions USING btree (event_id);


--
-- Name: ix_integration_ingestions_idempotency_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_integration_ingestions_idempotency_key ON public.integration_ingestions USING btree (idempotency_key);


--
-- Name: ix_integration_ingestions_knowledge_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_integration_ingestions_knowledge_id ON public.integration_ingestions USING btree (knowledge_id);


--
-- Name: ix_integration_ingestions_review_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_integration_ingestions_review_status ON public.integration_ingestions USING btree (review_status);


--
-- Name: ix_integration_ingestions_source_conversation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_integration_ingestions_source_conversation_id ON public.integration_ingestions USING btree (source_conversation_id);


--
-- Name: ix_integration_ingestions_source_system; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_integration_ingestions_source_system ON public.integration_ingestions USING btree (source_system);


--
-- Name: ix_integration_ingestions_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_integration_ingestions_status ON public.integration_ingestions USING btree (status);


--
-- Name: ix_knowledge_change_logs_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_change_logs_created_at ON public.knowledge_change_logs USING btree (created_at);


--
-- Name: ix_knowledge_change_logs_knowledge_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_change_logs_knowledge_id ON public.knowledge_change_logs USING btree (knowledge_id);


--
-- Name: ix_knowledge_deduplication_feedback_knowledge_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_deduplication_feedback_knowledge_id ON public.knowledge_deduplication_feedback USING btree (knowledge_id);


--
-- Name: ix_knowledge_deduplication_feedback_matched_knowledge_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_deduplication_feedback_matched_knowledge_id ON public.knowledge_deduplication_feedback USING btree (matched_knowledge_id);


--
-- Name: ix_knowledge_embeddings_content_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_embeddings_content_hash ON public.knowledge_embeddings USING btree (content_hash);


--
-- Name: ix_knowledge_embeddings_embedding_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_embeddings_embedding_model ON public.knowledge_embeddings USING btree (embedding_model);


--
-- Name: ix_knowledge_embeddings_knowledge_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_embeddings_knowledge_id ON public.knowledge_embeddings USING btree (knowledge_id);


--
-- Name: ix_knowledge_embeddings_vector_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_embeddings_vector_hnsw ON public.knowledge_embeddings USING hnsw (embedding_vector public.vector_cosine_ops);


--
-- Name: ix_knowledge_items_applicable_brands_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_items_applicable_brands_gin ON public.knowledge_items USING gin (((applicable_brands)::jsonb));


--
-- Name: ix_knowledge_items_applicable_categories_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_items_applicable_categories_gin ON public.knowledge_items USING gin (((applicable_categories)::jsonb));


--
-- Name: ix_knowledge_items_applicable_models_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_items_applicable_models_gin ON public.knowledge_items USING gin (((applicable_models)::jsonb));


--
-- Name: ix_knowledge_items_category_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_items_category_id ON public.knowledge_items USING btree (category_id);


--
-- Name: ix_knowledge_items_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_items_status ON public.knowledge_items USING btree (status);


--
-- Name: ix_knowledge_items_title; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_items_title ON public.knowledge_items USING btree (title);


--
-- Name: ix_knowledge_media_knowledge_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_media_knowledge_id ON public.knowledge_media USING btree (knowledge_id);


--
-- Name: ix_knowledge_search_embeddings_content_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_search_embeddings_content_hash ON public.knowledge_search_embeddings USING btree (content_hash);


--
-- Name: ix_knowledge_search_embeddings_embedding_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_search_embeddings_embedding_kind ON public.knowledge_search_embeddings USING btree (embedding_kind);


--
-- Name: ix_knowledge_search_embeddings_embedding_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_search_embeddings_embedding_model ON public.knowledge_search_embeddings USING btree (embedding_model);


--
-- Name: ix_knowledge_search_embeddings_knowledge_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_search_embeddings_knowledge_id ON public.knowledge_search_embeddings USING btree (knowledge_id);


--
-- Name: ix_knowledge_search_embeddings_vector_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_search_embeddings_vector_hnsw ON public.knowledge_search_embeddings USING hnsw (embedding_vector public.vector_cosine_ops);


--
-- Name: ix_knowledge_tags_knowledge_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_tags_knowledge_id ON public.knowledge_tags USING btree (knowledge_id);


--
-- Name: ix_knowledge_tags_tag_value_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_tags_tag_value_id ON public.knowledge_tags USING btree (tag_value_id);


--
-- Name: ix_media_deletion_tasks_next_attempt_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_media_deletion_tasks_next_attempt_at ON public.media_deletion_tasks USING btree (next_attempt_at);


--
-- Name: ix_media_deletion_tasks_storage_backend; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_media_deletion_tasks_storage_backend ON public.media_deletion_tasks USING btree (storage_backend);


--
-- Name: ix_media_upload_staging_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_media_upload_staging_expires_at ON public.media_upload_staging USING btree (expires_at);


--
-- Name: ix_media_upload_staging_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_media_upload_staging_username ON public.media_upload_staging USING btree (username);


--
-- Name: ix_retrieval_quality_events_conversation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_retrieval_quality_events_conversation_id ON public.retrieval_quality_events USING btree (conversation_id);


--
-- Name: ix_retrieval_quality_events_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_retrieval_quality_events_created_at ON public.retrieval_quality_events USING btree (created_at);


--
-- Name: ix_retrieval_quality_events_idempotency_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_retrieval_quality_events_idempotency_key ON public.retrieval_quality_events USING btree (idempotency_key);


--
-- Name: ix_retrieval_quality_events_outcome; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_retrieval_quality_events_outcome ON public.retrieval_quality_events USING btree (outcome);


--
-- Name: ix_retrieval_quality_events_query_text; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_retrieval_quality_events_query_text ON public.retrieval_quality_events USING btree (query_text);


--
-- Name: ix_retrieval_quality_events_source_system; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_retrieval_quality_events_source_system ON public.retrieval_quality_events USING btree (source_system);


--
-- Name: ix_retrieval_quality_events_top_knowledge_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_retrieval_quality_events_top_knowledge_id ON public.retrieval_quality_events USING btree (top_knowledge_id);


--
-- Name: ix_tag_values_dimension_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tag_values_dimension_id ON public.tag_values USING btree (dimension_id);


--
-- Name: ix_user_sessions_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_sessions_expires_at ON public.user_sessions USING btree (expires_at);


--
-- Name: ix_user_sessions_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_sessions_user_id ON public.user_sessions USING btree (user_id);


--
-- Name: ix_users_username; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_users_username ON public.users USING btree (username);


--
-- Name: categories categories_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT categories_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.categories(id);


--
-- Name: knowledge_change_logs knowledge_change_logs_knowledge_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_change_logs
    ADD CONSTRAINT knowledge_change_logs_knowledge_id_fkey FOREIGN KEY (knowledge_id) REFERENCES public.knowledge_items(id);


--
-- Name: knowledge_deduplication_feedback knowledge_deduplication_feedback_knowledge_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_deduplication_feedback
    ADD CONSTRAINT knowledge_deduplication_feedback_knowledge_id_fkey FOREIGN KEY (knowledge_id) REFERENCES public.knowledge_items(id);


--
-- Name: knowledge_deduplication_feedback knowledge_deduplication_feedback_matched_knowledge_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_deduplication_feedback
    ADD CONSTRAINT knowledge_deduplication_feedback_matched_knowledge_id_fkey FOREIGN KEY (matched_knowledge_id) REFERENCES public.knowledge_items(id);


--
-- Name: knowledge_embeddings knowledge_embeddings_knowledge_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_embeddings
    ADD CONSTRAINT knowledge_embeddings_knowledge_id_fkey FOREIGN KEY (knowledge_id) REFERENCES public.knowledge_items(id);


--
-- Name: knowledge_items knowledge_items_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_items
    ADD CONSTRAINT knowledge_items_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.categories(id);


--
-- Name: knowledge_media knowledge_media_knowledge_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_media
    ADD CONSTRAINT knowledge_media_knowledge_id_fkey FOREIGN KEY (knowledge_id) REFERENCES public.knowledge_items(id);


--
-- Name: knowledge_search_embeddings knowledge_search_embeddings_knowledge_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_search_embeddings
    ADD CONSTRAINT knowledge_search_embeddings_knowledge_id_fkey FOREIGN KEY (knowledge_id) REFERENCES public.knowledge_items(id);


--
-- Name: knowledge_tags knowledge_tags_knowledge_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_tags
    ADD CONSTRAINT knowledge_tags_knowledge_id_fkey FOREIGN KEY (knowledge_id) REFERENCES public.knowledge_items(id);


--
-- Name: knowledge_tags knowledge_tags_tag_value_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_tags
    ADD CONSTRAINT knowledge_tags_tag_value_id_fkey FOREIGN KEY (tag_value_id) REFERENCES public.tag_values(id);


--
-- Name: tag_values tag_values_dimension_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_values
    ADD CONSTRAINT tag_values_dimension_id_fkey FOREIGN KEY (dimension_id) REFERENCES public.tag_dimensions(id);


--
-- Name: usage_stats usage_stats_knowledge_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_stats
    ADD CONSTRAINT usage_stats_knowledge_id_fkey FOREIGN KEY (knowledge_id) REFERENCES public.knowledge_items(id);


--
-- Name: user_sessions user_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_sessions
    ADD CONSTRAINT user_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
--



-- 基础知识分类；不包含用户、知识、会话、暂存媒体或令牌。
INSERT INTO public.categories (id, name, parent_id, level, sort_order, created_at) VALUES
    ('cat-qc-standard', '质检标准', NULL, 1, 10, CURRENT_TIMESTAMP),
    ('cat-qc-process', '操作流程', NULL, 1, 20, CURRENT_TIMESTAMP),
    ('cat-case-analysis', '案例解析', NULL, 1, 30, CURRENT_TIMESTAMP),
    ('cat-extra-knowledge', '课外常识', NULL, 1, 40, CURRENT_TIMESTAMP);

-- 版本标记必须最后写入，避免结构未完成却被误判为已迁移。
INSERT INTO public.alembic_version (version_num) VALUES ('20260724_02');
COMMIT;
