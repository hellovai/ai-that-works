from fastapi import FastAPI, HTTPException, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
import uuid
from datetime import datetime
import os
from dotenv import load_dotenv

from models import (
    VideoImportRequest, DraftUpdateRequest, FeedbackRequest, ContentRefinementRequest, TitleUpdateRequest,
    Video, Draft, Feedback,
    VideoImportResponse, VideoResponse, SummaryResponse, 
    DraftsListResponse, DraftSaveResponse, FeedbackResponse, StatusResponse,
    ZoomRecordingsResponse, ZoomRecording,
    ZoomMeetingRecordings, ZoomMeetingsResponse, TranscriptResponse
)
from database import db
from zoom_client import zoom_client
from video_processor import video_processor
from baml_client import types
from baml_client.async_client import b

# Load environment variables
load_dotenv()

app = FastAPI(title="AI Content Pipeline API", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # Frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Validate required environment variables
required_env_vars = ["SUPABASE_URL", "SUPABASE_ANON_KEY"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    print(f"WARNING: Missing environment variables: {', '.join(missing_vars)}")

@app.get("/")
async def root():
    return {"message": "AI Content Pipeline API"}

@app.post("/videos/import", status_code=status.HTTP_202_ACCEPTED, response_model=VideoImportResponse)
async def import_video(request: VideoImportRequest, background_tasks: BackgroundTasks):
    """Queue Zoom download - returns video ID immediately and starts full background processing pipeline"""
    video_id = str(uuid.uuid4())
    
    # Create video record
    video = Video(
        id=video_id,
        zoom_meeting_id=request.zoom_meeting_id,
        title=f"Zoom Meeting {request.zoom_meeting_id}",
        duration=3600,  # 1 hour
        status="processing",
        processing_stage="queued",
        created_at=datetime.now()
    )
    
    try:
        await db.create_video(video)
        
        # Add background task for complete video processing pipeline
        background_tasks.add_task(complete_video_processing_pipeline, video_id, request.zoom_meeting_id)
        
        return VideoImportResponse(video_id=video_id, status="queued")
    except Exception as e:
        print(f"Error creating video: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

async def complete_video_processing_pipeline(video_id: str, zoom_meeting_id: str):
    """Complete background processing pipeline: download video + upload to YouTube + auto-summarize + generate content"""
    try:
        print(f"🚀 Starting complete processing pipeline for video {video_id}")
        
        # Step 1: Process video (download, upload to YouTube, get transcript)
        await video_processor.process_video(video_id, zoom_meeting_id)
        
        # Step 2: Get the updated video with transcript
        video = await db.get_video(video_id)
        if not video:
            print(f"❌ Video {video_id} not found after processing")
            return
        
        # Step 3: Auto-trigger summarization if transcript is available
        if video.transcript:
            print(f"🧠 Auto-triggering summarization for video {video_id}")
            await process_video_summary(video_id, video.transcript, video.title)
        else:
            print(f"⚠️ No transcript available for video {video_id}, skipping auto-summarization")
            
        print(f"✅ Complete processing pipeline finished for video {video_id}")
        
    except Exception as e:
        print(f"❌ Error in complete processing pipeline for video {video_id}: {e}")
        import traceback
        traceback.print_exc()
        # Update video status to failed
        await db.update_video(video_id, {
            "status": "failed",
            "processing_stage": "pipeline_failed"
        })

@app.get("/videos/{video_id}", response_model=VideoResponse)
async def get_video(video_id: str):
    """Get video details + drafts"""
    try:
        video = await db.get_video(video_id)
        if not video:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        
        video_drafts = await db.get_drafts_by_video(video_id)
        return VideoResponse(video=video, drafts=video_drafts)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error getting video {video_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@app.post("/videos/{video_id}/summarize", status_code=status.HTTP_202_ACCEPTED, response_model=StatusResponse)
async def trigger_summarize(video_id: str, background_tasks: BackgroundTasks):
    """Trigger BAML summarization pipeline"""
    try:
        video = await db.get_video(video_id)
        if not video:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        
        if not video.transcript:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Video transcript not available for summarization")
        
        # Add background task for summarization
        background_tasks.add_task(process_video_summary, video_id, video.transcript, video.title)
        
        # Update status to processing with detailed stage
        await db.update_video(video_id, {
            "status": "processing",
            "processing_stage": "summarizing"
        })
        return StatusResponse(status="summarization started")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error triggering summarize for video {video_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


async def process_video_summary(video_id: str, transcript: str, title: Optional[str] = None):
    """Background task to process video summary and generate content using BAML with parallel processing"""
    try:
        print(f"🚀 Starting BAML summarization for video {video_id}")
        
        # Step 1: Generate video summary FIRST
        stream = b.stream.SummarizeVideo(transcript=transcript, title=title)
        async for video_summary in stream:
            summary_data = video_summary.model_dump(mode="json")
            summary_data["generated_at"] = datetime.now().isoformat()
            await db.update_video(video_id, {
                "summary": summary_data,
                "summary_points": video_summary.bullet_points,
                "processing_stage": "summarizing"
            })
        video_summary = await stream.get_final_response()
        print(f"✅ BAML summarization completed for video {video_id}")
        
        # Step 2: Save summary to DB immediately and delete prior drafts
        summary_data = video_summary.model_dump(mode="json")
        summary_data["generated_at"] = datetime.now().isoformat()
        
        # Delete all existing drafts for this video (fresh start)
        print(f"🗑️ Deleting all existing drafts for video {video_id}")
        await db.delete_drafts_by_video(video_id)
        
        await db.update_video(video_id, {
            "summary": summary_data,
            "summary_points": video_summary.bullet_points,
            "processing_stage": "generating_content"
        })
        print(f"💾 Summary saved for video {video_id}, UI updated immediately!")
        
        # Step 3: Generate YouTube title using BAML
        print(f"🎬 Generating YouTube title for video {video_id}")
        try:
            new_title = await b.GenerateYouTubeTitle(
                summary=video_summary,
                transcript=transcript,
                current_title=title
            )
            await db.update_video(video_id, {"title": new_title})
            print(f"✅ YouTube title generated and updated: {new_title}")
        except Exception as e:
            print(f"❌ Error generating title: {e}")
            # Continue with original title if generation fails
        
        # Step 4: Create a single draft and update it as content generates
        print(f"🔄 Starting parallel content generation for video {video_id}")
        
        # Create a shared draft record first
        shared_draft_id = str(uuid.uuid4())
        initial_draft = Draft(
            id=shared_draft_id,
            video_id=video_id,
            email_draft=None,
            x_draft=None,
            linkedin_draft=None,
            created_at=datetime.now(),
            version=1
        )
        
        await db.create_draft(initial_draft)
        print(f"📝 Created shared draft {shared_draft_id} for video {video_id}")
        
        # Create tasks for parallel execution that update the same draft
        import asyncio
        
        async def generate_and_update_email():
            try:
                print(f"📧 Generating email draft for video {video_id}")
                # Get updated video to use latest title
                updated_video = await db.get_video(video_id)
                structure: types.EmailStructure = await b.GenerateEmailDraft(
                    summary=video_summary,
                    transcript=transcript,
                    video_title=updated_video.title if updated_video else title
                )

                email_draft = await b.GenerateEmailStructure(
                    summary=video_summary,
                    structure=structure
                )
                
                # Update the shared draft with email content
                from models import EmailDraftContent
                email_draft_content = EmailDraftContent(
                    subject=email_draft.subject,
                    body=email_draft.body,
                    call_to_action="<none>"
                )
                
                await db.update_draft_field(shared_draft_id, "email_draft", email_draft_content)
                print(f"✅ Email content updated in shared draft {shared_draft_id} - UI will update in real-time!")
                
            except Exception as e:
                print(f"❌ Error generating email draft: {e}")
        
        async def generate_and_update_x():
            try:
                print(f"🐦 Generating X thread for video {video_id}")
                # Get updated video to use latest title
                updated_video = await db.get_video(video_id)
                twitter_thread: types.TwitterThread = await b.GenerateTwitterThread(
                    summary=video_summary,
                    video_title=updated_video.title if updated_video else title
                )
                
                # Update the shared draft with X content
                from models import XDraftContent
                x_draft_content = XDraftContent(
                    tweets=twitter_thread.tweets,
                    hashtags=twitter_thread.hashtags
                )
                
                await db.update_draft_field(shared_draft_id, "x_draft", x_draft_content)
                print(f"✅ X content updated in shared draft {shared_draft_id} - UI will update in real-time!")
                
            except Exception as e:
                print(f"❌ Error generating X draft: {e}")
        
        async def generate_and_update_linkedin():
            try:
                print(f"💼 Generating LinkedIn post for video {video_id}")
                # Get updated video to use latest title
                updated_video = await db.get_video(video_id)
                linkedin_post: types.LinkedInPost = await b.GenerateLinkedInPost(
                    summary=video_summary,
                    video_title=updated_video.title if updated_video else title
                )
                
                # Update the shared draft with LinkedIn content
                from models import LinkedInDraftContent
                linkedin_draft_content = LinkedInDraftContent(
                    content=linkedin_post.content,
                    hashtags=linkedin_post.hashtags
                )
                
                await db.update_draft_field(shared_draft_id, "linkedin_draft", linkedin_draft_content)
                print(f"✅ LinkedIn content updated in shared draft {shared_draft_id} - UI will update in real-time!")
                
            except Exception as e:
                print(f"❌ Error generating LinkedIn draft: {e}")
        
        # Execute all content generation in parallel
        await asyncio.gather(
            generate_and_update_email(),
            generate_and_update_x(),
            generate_and_update_linkedin(),
            return_exceptions=True  # Don't fail if one content type fails
        )
        
        print(f"🎉 All content generation completed for video {video_id}")
        
        # Finalize video status
        await db.update_video(video_id, {
            "status": "ready",
            "processing_stage": "completed"
        })
        print(f"✅ Video {video_id} processing completed successfully")
        
    except Exception as e:
        print(f"❌ Error processing summary for video {video_id}: {e}")
        # Update video status to failed
        await db.update_video(video_id, {
            "status": "failed",
            "processing_stage": "summary_failed"
        })

@app.get("/videos/{video_id}/summary", response_model=SummaryResponse)
async def get_summary(video_id: str):
    """Get summary points"""
    try:
        video = await db.get_video(video_id)
        if not video:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        
        return SummaryResponse(summary_points=video.summary_points or [])
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error getting summary for video {video_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@app.get("/videos/{video_id}/transcript", response_model=TranscriptResponse)
async def get_transcript(video_id: str):
    """Get video transcript"""
    try:
        video = await db.get_video(video_id)
        if not video:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        
        if not video.transcript:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transcript not available")
        
        return TranscriptResponse(transcript=video.transcript)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error getting transcript for video {video_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@app.get("/videos/{video_id}/drafts", response_model=DraftsListResponse)
async def list_drafts(video_id: str):
    """List draft history"""
    try:
        video = await db.get_video(video_id)
        if not video:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        
        video_drafts = await db.get_drafts_by_video(video_id)
        return DraftsListResponse(drafts=video_drafts)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error listing drafts for video {video_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@app.post("/videos/{video_id}/drafts", response_model=DraftSaveResponse)
async def save_drafts(video_id: str, request: DraftUpdateRequest):
    """Save edited drafts"""
    print(f"🎯 Save drafts endpoint called for video: {video_id}")
    print(f"📝 Request data: {request}")
    
    try:
        video = await db.get_video(video_id)
        if not video:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        
        draft_id = str(uuid.uuid4())
        
        # Get existing drafts to determine version number
        existing_drafts = await db.get_drafts_by_video(video_id)
        new_version = max([d.version for d in existing_drafts], default=0) + 1
        
        # Create new draft
        draft = Draft(
            id=draft_id,
            video_id=video_id,
            email_draft=request.email_draft,
            x_draft=request.x_draft,
            linkedin_draft=request.linkedin_draft,
            created_at=datetime.now(),
            version=new_version
        )
        
        await db.create_draft(draft)
        print(f"✅ Draft saved successfully: {draft_id}")
        return DraftSaveResponse(draft_id=draft_id, status="saved")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error saving draft for video {video_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@app.post("/drafts/{draft_id}/feedback", response_model=FeedbackResponse)
async def add_feedback(draft_id: str, request: FeedbackRequest):
    """Add feedback"""
    try:
        draft = await db.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
        
        feedback_id = str(uuid.uuid4())
        
        feedback = Feedback(
            id=feedback_id,
            draft_id=draft_id,
            content=request.content,
            created_at=datetime.now()
        )
        
        await db.create_feedback(feedback)
        return FeedbackResponse(feedback_id=feedback_id, status="added")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error adding feedback for draft {draft_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@app.post("/videos/{video_id}/refine-content", response_model=StatusResponse)
async def refine_content(video_id: str, request: ContentRefinementRequest, background_tasks: BackgroundTasks):
    """Refine content based on user feedback using BAML - returns immediately, processes in background"""
    print(f"🎯 Content refinement called for video: {video_id}")
    print(f"📝 Feedback: {request.feedback}")
    print(f"🎨 Content type: {request.content_type}")
    
    try:
        # Validate video exists
        video = await db.get_video(video_id)
        if not video:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        
        # Validate current draft content is provided
        if not request.current_draft:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current draft content is required")
        
        # Validate content type
        if request.content_type not in ["email", "x", "linkedin"]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid content_type. Must be 'email', 'x', or 'linkedin'")
        
        # Create placeholder draft immediately for fast response
        draft_id = str(uuid.uuid4())
        existing_drafts = await db.get_drafts_by_video(video_id)
        new_version = max([d.version for d in existing_drafts], default=0) + 1
        
        # Get the latest draft to preserve other content types
        latest_draft = existing_drafts[0] if existing_drafts else None
        
        # Create placeholder draft preserving existing content
        from models import EmailDraftContent, XDraftContent, LinkedInDraftContent
        
        # Start with existing content from latest draft
        email_draft = latest_draft.email_draft if latest_draft else None
        x_draft = latest_draft.x_draft if latest_draft else None
        linkedin_draft = latest_draft.linkedin_draft if latest_draft else None
        
        # Set the content being refined to current version (will be updated in background)
        if request.content_type == "email":
            email_draft = EmailDraftContent(**request.current_draft)
        elif request.content_type == "x":
            x_draft = XDraftContent(**request.current_draft)
        elif request.content_type == "linkedin":
            linkedin_draft = LinkedInDraftContent(**request.current_draft)
        
        placeholder_draft = Draft(
            id=draft_id,
            video_id=video_id,
            email_draft=email_draft,
            x_draft=x_draft,
            linkedin_draft=linkedin_draft,
            created_at=datetime.now(),
            version=new_version
        )
        
        await db.create_draft(placeholder_draft)
        print(f"✅ Placeholder draft created: {draft_id}")
        
        # Add background task to refine content
        background_tasks.add_task(
            refine_content_background_task,
            video_id,
            draft_id,
            request.content_type,
            request.feedback,
            request.current_draft
        )
        
        print(f"🚀 Background refinement task started for draft {draft_id}")
        return StatusResponse(status="OK")
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error starting content refinement for video {video_id}: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

async def refine_content_background_task(
    video_id: str,
    draft_id: str,
    content_type: str,
    feedback: str,
    current_draft_data: dict
):
    """Background task to refine content using BAML"""
    print(f"🔄 Starting background refinement for draft {draft_id} ({content_type})")
    
    try:
        # Get video and its data for context
        video = await db.get_video(video_id)
        if not video:
            print(f"❌ Video {video_id} not found during background refinement")
            return
        
        # Get video summary for context
        video_summary = None
        if hasattr(video, 'summary') and video.summary:
            # Convert dict summary to BAML VideoSummary type
            video_summary = types.VideoSummary(
                bullet_points=video.summary.get('bullet_points', []),
                key_topics=video.summary.get('key_topics', []),
                main_takeaways=video.summary.get('main_takeaways', []),
                timed_data=video.summary.get('timed_data', [])
            )
        elif video.summary_points:
            # Fallback to legacy format
            video_summary = types.VideoSummary(
                bullet_points=video.summary_points,
                key_topics=[],
                main_takeaways=[],
                timed_data=[]
            )
        else:
            print(f"❌ No video summary available for video {video_id}")
            return
        
        # Refine content based on type using BAML
        refined_content = None
        
        if content_type == "email":
            current_email = types.EmailDraft(**current_draft_data)
            print(f"📧 Refining email content with BAML...")
            refined_content = await b.RefineEmailDraft(
                current_draft=current_email,
                feedback=feedback,
                summary=video_summary,
                transcript=video.transcript,
                video_title=video.title
            )
            
            # Update the draft with refined email content
            from models import EmailDraftContent
            refined_email = EmailDraftContent(
                subject=refined_content.subject,
                body=refined_content.body,
                call_to_action="<none>"
            )
            await db.update_draft_field(draft_id, "email_draft", refined_email)
            
        elif content_type == "x":
            current_x = types.TwitterThread(**current_draft_data)
            print(f"🐦 Refining X thread content with BAML...")
            refined_content = await b.RefineTwitterThread(
                current_draft=current_x,
                feedback=feedback,
                summary=video_summary,
                transcript=video.transcript,
                video_title=video.title
            )
            
            # Update the draft with refined X content
            from models import XDraftContent
            refined_x = XDraftContent(
                tweets=refined_content.tweets,
                hashtags=refined_content.hashtags
            )
            await db.update_draft_field(draft_id, "x_draft", refined_x)
            
        elif content_type == "linkedin":
            current_linkedin = types.LinkedInPost(**current_draft_data)
            print(f"💼 Refining LinkedIn post content with BAML...")
            refined_content = await b.RefineLinkedInPost(
                current_draft=current_linkedin,
                feedback=feedback,
                summary=video_summary,
                transcript=video.transcript,
                video_title=video.title
            )
            
            # Update the draft with refined LinkedIn content
            from models import LinkedInDraftContent
            refined_linkedin = LinkedInDraftContent(
                content=refined_content.content,
                hashtags=refined_content.hashtags
            )
            await db.update_draft_field(draft_id, "linkedin_draft", refined_linkedin)
        
        print(f"✅ Background refinement completed for draft {draft_id} ({content_type})")
        print(f"🔔 Real-time update will notify frontend of changes")
        
    except Exception as e:
        print(f"❌ Error in background refinement for draft {draft_id}: {e}")
        import traceback
        traceback.print_exc()

@app.post("/videos/{video_id}/generate-title", response_model=StatusResponse)
async def generate_video_title(video_id: str, background_tasks: BackgroundTasks):
    """Generate a new YouTube title for the video using BAML"""
    print(f"🎬 Generating YouTube title for video: {video_id}")
    
    try:
        # Validate video exists
        video = await db.get_video(video_id)
        if not video:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        
        # Add background task to generate title
        background_tasks.add_task(generate_title_background_task, video_id)
        
        print(f"🚀 Background title generation task started for video {video_id}")
        return StatusResponse(status="OK")
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error starting title generation for video {video_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@app.put("/videos/{video_id}/title", response_model=StatusResponse)
async def update_video_title(video_id: str, request: TitleUpdateRequest):
    """Update video title manually"""
    print(f"📝 Updating title for video {video_id}: {request.title}")
    
    try:
        # Validate video exists
        video = await db.get_video(video_id)
        if not video:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
        
        # Update title
        await db.update_video(video_id, {"title": request.title})
        
        print(f"✅ Title updated successfully for video {video_id}")
        return StatusResponse(status="OK")
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error updating title for video {video_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

async def generate_title_background_task(video_id: str):
    """Background task to generate YouTube title using BAML"""
    print(f"🔄 Starting background title generation for video {video_id}")
    
    try:
        # Get video and its data
        video = await db.get_video(video_id)
        if not video:
            print(f"❌ Video {video_id} not found during title generation")
            return
        
        # Get video summary for context
        video_summary = None
        if hasattr(video, 'summary') and video.summary:
            video_summary = types.VideoSummary(
                bullet_points=video.summary.get('bullet_points', []),
                key_topics=video.summary.get('key_topics', []),
                main_takeaways=video.summary.get('main_takeaways', []),
                timed_data=video.summary.get('timed_data', [])
            )
        elif video.summary_points:
            video_summary = types.VideoSummary(
                bullet_points=video.summary_points,
                key_topics=[],
                main_takeaways=[],
                timed_data=[]
            )
        else:
            print(f"❌ No video summary available for video {video_id}")
            return
        
        # Generate new title using BAML
        print(f"🎬 Generating YouTube title with BAML...")
        new_title = await b.GenerateYouTubeTitle(
            summary=video_summary,
            transcript=video.transcript,
            current_title=video.title
        )
        
        # Update the video with new title
        await db.update_video(video_id, {"title": new_title})
        
        print(f"✅ Background title generation completed for video {video_id}")
        print(f"📝 New title: {new_title}")
        print(f"🔔 Real-time update will notify frontend of changes")
        
    except Exception as e:
        print(f"❌ Error in background title generation for video {video_id}: {e}")
        import traceback
        traceback.print_exc()

@app.get("/test/supabase")
async def test_supabase():
    """Test Supabase connection and credentials"""
    try:
        # Test database connection by trying to get a count
        from database import db
        # Try a simple operation to test connection
        db.client.table("videos").select("count").execute()
        return {
            "status": "connected", 
            "message": "Supabase credentials valid",
            "tables_accessible": True
        }
    except Exception as e:
        print(f"Supabase test failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"Supabase connection failed: {str(e)}"
        )

@app.get("/test/zoom")  
async def test_zoom():
    """Test Zoom API credentials"""
    zoom_account_id = os.getenv("ZOOM_ACCOUNT_ID")
    zoom_client_id = os.getenv("ZOOM_CLIENT_ID")
    zoom_client_secret = os.getenv("ZOOM_CLIENT_SECRET")
    
    if not zoom_account_id or not zoom_client_id or not zoom_client_secret:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                          detail="Zoom OAuth credentials not configured")
    
    try:
        # Test the Zoom client
        recordings = zoom_client.get_recordings()
        return {
            "status": "configured", 
            "message": "Zoom OAuth credentials valid",
            "recordings_count": len(recordings)
        }
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                          detail=f"Zoom API test failed: {str(e)}")

@app.get("/zoom/recordings", response_model=ZoomMeetingsResponse)
async def get_zoom_recordings(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    user_id: str = "me"
):
    """Fetch existing Zoom recordings, grouped by meeting"""
    try:
        recordings_data = zoom_client.get_recordings(
            user_id=user_id,
            from_date=from_date,
            to_date=to_date
        )
        # Group by meeting_id
        meetings = {}
        for rec in recordings_data:
            m_id = rec["meeting_id"]
            if m_id not in meetings:
                meetings[m_id] = {
                    "meeting_id": m_id,
                    "meeting_title": rec["meeting_title"],
                    "recording_start": rec["recording_start"],
                    "recording_end": rec["recording_end"],
                    "recordings": []
                }
            meetings[m_id]["recordings"].append(ZoomRecording(**rec))
        meetings_list = [ZoomMeetingRecordings(**m) for m in meetings.values()]
        return ZoomMeetingsResponse(
            meetings=meetings_list,
            total_count=len(meetings_list)
        )
    except Exception as e:
        print(f"Error fetching Zoom recordings: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch Zoom recordings: {str(e)}"
        )

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)