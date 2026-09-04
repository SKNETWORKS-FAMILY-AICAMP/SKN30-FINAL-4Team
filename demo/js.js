let loadingOverlay = null;
let stepUp = null;
let stepResult = null;
let fileInput = null;
let chatContainer = null;
let aiChat = null;

if ('scrollRestoration' in history) {
    history.scrollRestoration = 'manual';
}

document.addEventListener("DOMContentLoaded", () => {
    loadingOverlay = document.getElementById("loading-overlay");
    stepUp = document.getElementById("step-up");
    stepResult = document.getElementById("step-result");
    chatContainer = document.querySelector("#ai-chat .overflow-y-auto");
    aiChat = document.getElementById("ai-chat");

    window.scrollTo({
        top: 0,
        behavior: "instant"
    });
    
    chatContainerScrollToBottom();
});

function getFileInput() {
    return document.querySelector("#upload-box input[type='file']") || fileInput;
}

function handleFileChange(event) {
    const file = event.target.files[0];
    if (!file) return;

    const maxSize = 50 * 1024 * 1024;
    if (file.size > maxSize) {
        alert("파일 크기가 50MB를 초과했습니다.");
        const currentInput = getFileInput();
        if (currentInput) currentInput.value = "";
        return;
    }

    if (loadingOverlay) {
        loadingOverlay.classList.remove("hidden");
    }

    document.addEventListener("click", handleDocumentClick, { once: true });
}

function handleDocumentClick(event) {
    hideLoading();
}

function hideLoading() {
    if (loadingOverlay) {
        loadingOverlay.classList.add("hidden");
    }
    
    const currentInput = getFileInput();
    if (currentInput) {
        currentInput.value = "";
    }

    if (stepUp) {
        stepUp.classList.add("hidden");
    }
    if (stepResult) {
        stepResult.classList.remove("hidden");
    }

    window.scrollTo({
        top: 0,
        behavior: "smooth"
    });
}

function showStepUp() {
    if (stepResult) {
        stepResult.classList.add("hidden");
    }
    if (stepUp) {
        stepUp.classList.remove("hidden");
    }

    if (loadingOverlay) {
        loadingOverlay.classList.add("hidden");
    }
    
    const currentInput = getFileInput();
    if (currentInput) {
        currentInput.value = "";
    }

    if (aiChat) {
        aiChat.classList.remove("translate-y-0");
        aiChat.classList.add("translate-y-[calc(100%-64px)]");
    }

    window.scrollTo({
        top: 0,
        behavior: "smooth"
    });
}

function toggleAiChat() {
    if (!aiChat) return;

    const isClosed = aiChat.classList.contains("translate-y-[calc(100%-64px)]");

    if (isClosed) {
        aiChat.classList.remove("translate-y-[calc(100%-64px)]");
        aiChat.classList.add("translate-y-0");

        chatContainerScrollToBottom()
    } else {
        aiChat.classList.remove("translate-y-0");
        aiChat.classList.add("translate-y-[calc(100%-64px)]");
    }
}

function chatContainerScrollToBottom() {
    if (chatContainer) {
        chatContainer.scrollTop = chatContainer.scrollHeight;
    }
}