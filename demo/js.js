function handleFileChange(event) {
    const file = event.target.files[0];
    if (!file) return;

    // 1. 파일 크기 검증 (최대 50MB)
    const maxSize = 50 * 1024 * 1024;
    if (file.size > maxSize) {
        alert("파일 크기가 50MB를 초과했습니다.");
        event.target.value = ""; // input 초기화
        return;
    }

    // 2. 딤 & 스피너 표시
    const loadingOverlay = document.getElementById("loading-overlay");
    loadingOverlay.classList.remove("hidden");

    console.log("선택된 파일:", file.name);

    // 3. 아무 곳이나 클릭했을 때 hideLoading 실행
    document.addEventListener("click", handleDocumentClick, { once: true });
}

// 문서 클릭 시 실행될 핸들러 함수
function handleDocumentClick(event) {
    hideLoading();
}

// 로딩 화면 숨김 처리 및 단계 전환 함수
function hideLoading() {
    const loadingOverlay = document.getElementById("loading-overlay");
    if (loadingOverlay) {
        loadingOverlay.classList.add("hidden");
    }

    // #step-up은 숨기고, #step-result는 보이게 처리
    const stepUp = document.getElementById("step-up");
    const stepResult = document.getElementById("step-result");

    if (stepUp) {
        stepUp.classList.add("hidden");
    }
    if (stepResult) {
        stepResult.classList.remove("hidden");
    }
}

function toggleAiChat() {
    const aiChat = document.getElementById("ai-chat");
    const aiChatIcon = document.getElementById("ai-chat-icon");

    // 현재 닫혀있는지(아래로 내려가 있는지) 확인
    const isClosed = aiChat.classList.contains("translate-y-[calc(100%-64px)]");

    if (isClosed) {
        // 열기: 아래로 밀려있던 걸 0으로 올려서 전체가 보이게 함
        aiChat.classList.remove("translate-y-[calc(100%-64px)]");
        aiChat.classList.add("translate-y-0");
        // 화살표 아이콘 아래로 회전
        aiChatIcon.style.transform = "rotate(180deg)";
    } else {
        // 닫기: 다시 아래로 숨김
        aiChat.classList.remove("translate-y-0");
        aiChat.classList.add("translate-y-[calc(100%-64px)]");
        // 화살표 아이콘 원위치
        aiChatIcon.style.transform = "rotate(0deg)";
    }
}